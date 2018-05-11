import copy
import torch
import os
import os.path as osp
from scripts.configurations import voc_cfg
from torchfcn import script_utils
from torchfcn.datasets.voc import ALL_VOC_CLASS_NAMES
from torchfcn.models import model_utils
from scripts.configurations.sampler_cfg import sampler_cfgs


def test(frozen=True):
    gpu = 0
    os.environ['CUDA_VISIBLE_DEVICES'] = str(gpu)
    cuda = torch.cuda.is_available()

    cfg = voc_cfg.default_config
    cfg_override_args = {'n_instances_per_class': 1, 'max_iteration': 1, 'interval_validate': 1, 'lr': 0.01}
    for k, v in cfg_override_args.items():
        cfg.pop(k)  # ensures the key actually existed before
        cfg[k] = v

    problem_config = script_utils.get_problem_config(ALL_VOC_CLASS_NAMES, cfg['n_instances_per_class'])
    model, start_epoch, start_iteration = script_utils.get_model(cfg, problem_config,
                                                                 checkpoint=None, semantic_init=None, cuda=cuda)
    initial_model = copy.deepcopy(model)
    # script_utils.check_clean_work_tree()
    if frozen:
        model_utils.freeze_all_children(model)
        for module_name, module in model.named_children():
            assert all([p.requires_grad is False for p in module.parameters()]), '{} not frozen'.format(module_name)

    sampler_cfg = sampler_cfgs['default']
    sampler_cfg['train']['n_images'] = 1
    sampler_cfg['train_for_val']['n_images'] = 1
    sampler_cfg['val']['n_images'] = 1
    dataloaders = script_utils.get_dataloaders(cfg, 'voc', cuda, sampler_cfg)

    optim = script_utils.get_optimizer(cfg, model, None)
    out_dir = '/tmp/{}'.format(osp.basename(__file__))
    trainer = script_utils.get_trainer(cfg, cuda, model, optim, dataloaders, problem_config, out_dir=out_dir)
    trainer.epoch = start_epoch
    trainer.iteration = start_iteration
    trainer.train()
    state1 = initial_model.state_dict()
    state2 = model.state_dict()
    if frozen:
        assert set(state1.keys()) == set(state2.keys()), 'Debug Error'
        for param_name in state1.keys():
            assert torch.equal(state1[param_name], state2[param_name])
            print('Confirmed that the network does not learn when frozen')
    else:
        assert not torch.equal(state1['conv1_1.weight'], state2['conv1_1.weight'])
        print('Confirmed that the network does learn (when weights aren''t frozen)')


def main():
    test(frozen=False)
    test(frozen=True)


if __name__ == '__main__':
    main()
