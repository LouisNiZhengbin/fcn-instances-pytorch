#!/usr/bin/env python

import argparse
import os
import os.path as osp

import torch
from tensorboardX import SummaryWriter

import torchfcn
import torchfcn.datasets.voc
from torchfcn import script_utils
from torchfcn import instance_utils

default_config = dict(
    max_iteration=100000,
    lr=1.0e-12,
    momentum=0.99,
    weight_decay=0.0005,
    interval_validate=4000,
    matching=True,
    semantic_only_labels=False,
    n_instances_per_class=1,
    set_extras_to_void=True
)

configurations = {
    # same configuration as original work
    # https://github.com/shelhamer/fcn.berkeleyvision.org
    1: dict(
        n_instances_per_class=1,
        set_extras_to_void=False
    ),
    2: dict(
        semantic_only_labels=True,
        n_instances_per_class=1,
        set_extras_to_void=False
    ),
    3: dict(
        semantic_only_labels=False,
        n_instances_per_class=3,
        set_extras_to_void=True
    )
}

here = osp.dirname(osp.abspath(__file__))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-g', '--gpu', type=int, required=True)
    parser.add_argument('-c', '--config', type=int, default=1,
                        choices=configurations.keys())
    parser.add_argument('--resume', help='Checkpoint path')
    args = parser.parse_args()
    gpu = args.gpu
    config_idx = args.config

    cfg = script_utils.create_config_from_default(configurations[config_idx], default_config)

    out = script_utils.get_log_dir(osp.basename(__file__).replace(
        '.py', ''), config_idx, script_utils.create_config_copy(cfg),
        parent_directory=osp.dirname(osp.abspath(__file__)))

    print('logdir: {}'.format(out))
    resume = args.resume

    os.environ['CUDA_VISIBLE_DEVICES'] = str(gpu)
    cuda = torch.cuda.is_available()

    torch.manual_seed(1337)
    if cuda:
        torch.cuda.manual_seed(1337)

    # 0. Problem setup (instance segmentation definition)
    n_semantic_classes = 21
    n_instances_by_semantic_id = [1] + [cfg['n_instances_per_class'] for sem_cls in range(1, n_semantic_classes)]
    problem_config = instance_utils.InstanceProblemConfig(n_instances_by_semantic_id=n_instances_by_semantic_id)

    # 1. dataset
    root = osp.expanduser('~/data/datasets')
    dataset_kwargs = dict(transform=True, semantic_only_labels=cfg['semantic_only_labels'],
                          set_extras_to_void=cfg['set_extras_to_void'])
    kwargs = {'num_workers': 4, 'pin_memory': True} if cuda else {}
    
    train_loader = torch.utils.data.DataLoader(
        # torchfcn.datasets.SBDClassSeg(root, split='train', **dataset_kwargs), # Can't use SBD for instance (I believe)
       torchfcn.datasets.voc.VOC2011ClassSeg(root, split='train', **dataset_kwargs),
        batch_size=1, shuffle=True, **kwargs)
    # train_loader = torch.utils.data.DataLoader(
    #     torchfcn.datasets.SBDClassSeg(root, split='train', transform=True),
    #     batch_size=1, shuffle=True, **kwargs)
    val_loader = torch.utils.data.DataLoader(
        torchfcn.datasets.voc.VOC2011ClassSeg(
            root, split='seg11valid', **dataset_kwargs),
        batch_size=1, shuffle=False, **kwargs)

    # 2. model

    model = torchfcn.models.FCN8sInstanceAtOnce(semantic_instance_class_list=problem_config.semantic_instance_class_list,
                                                map_to_semantic=False)
    print('Number of classes in model: {}'.format(model.n_classes))
    start_epoch = 0
    start_iteration = 0
    if resume:
        checkpoint = torch.load(resume)
        model.load_state_dict(checkpoint['model_state_dict'])
        start_epoch = checkpoint['epoch']
        start_iteration = checkpoint['iteration']
    else:
        print('Copying params from vgg16')
        vgg16 = torchfcn.models.VGG16(pretrained=True)
        model.copy_params_from_vgg16(vgg16)
    if cuda:
        model = model.cuda()

    # 3. optimizer

    optim = torch.optim.SGD(
        [
            {'params': script_utils.get_parameters(model, bias=False)},
            {'params': script_utils.get_parameters(model, bias=True),
#            {'params': filter(lambda p: False if p is None else p.requires_grad, get_parameters(
#                model, bias=False))},
#            {'params': filter(lambda p: False if p is None else p.requires_grad, get_parameters(
#                model, bias=True)),

             'lr': cfg['lr'] * 2, 'weight_decay': 0},
        ],
        lr=cfg['lr'],
        momentum=cfg['momentum'],
        weight_decay=cfg['weight_decay'])
    if resume:
        optim.load_state_dict(checkpoint['optim_state_dict'])

    writer = SummaryWriter(log_dir=out)
    trainer = torchfcn.Trainer(
        cuda=cuda,
        model=model,
        optimizer=optim,
        train_loader=train_loader,
        val_loader=val_loader,
        out=out,
        max_iter=cfg['max_iteration'],
        interval_validate=cfg.get('interval_validate', len(train_loader)),
        tensorboard_writer=writer,
        matching_loss=cfg['matching'],
        loader_semantic_lbl_only=cfg['semantic_only_labels']
    )
    trainer.epoch = start_epoch
    trainer.iteration = start_iteration
    trainer.train()


if __name__ == '__main__':
    main()