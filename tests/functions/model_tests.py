import os

import torch

from scripts.configurations import voc_cfg
from torchfcn import script_utils
from torchfcn.datasets.voc import ALL_VOC_CLASS_NAMES
from torchfcn.models import model_utils


def build_example_model(**model_cfg_override_kwargs):
    # script_utils.check_clean_work_tree()
    gpu = 0
    os.environ['CUDA_VISIBLE_DEVICES'] = str(gpu)
    cuda = torch.cuda.is_available()

    cfg = voc_cfg.default_config
    for k, v in model_cfg_override_kwargs.items():
        cfg[k] = v
    problem_config = script_utils.get_problem_config(ALL_VOC_CLASS_NAMES, 2, map_to_semantic=cfg['map_to_semantic'])
    model, start_epoch, start_iteration = script_utils.get_model(cfg, problem_config,
                                                                 checkpoint=None, semantic_init=None, cuda=cuda)
    return model


def test_forward_hook():
    model = build_example_model()
    cfg = voc_cfg.default_config
    print('Getting datasets')
    gpu = 0
    os.environ['CUDA_VISIBLE_DEVICES'] = str(gpu)
    cuda = torch.cuda.is_available()
    torch.manual_seed(1337)
    if cuda:
        torch.cuda.manual_seed(1337)
    dataloaders = script_utils.get_dataloaders(cfg, 'voc', cuda, sampler_cfg=None)

    layer_names = ['conv1x1_instance_to_semantic'] if model.map_to_semantic else []
    layer_names += ['upscore8', 'score_pool4']
    activations = None
    for i, (x, y) in enumerate(dataloaders['train']):
        activations = model.get_activations(torch.autograd.Variable(x.cuda()), layer_names)
        if i >= 2:
            break
    assert set(activations.keys()) == set(layer_names)
    try:
        [activations[k].size() for k in activations.keys()]
    except:
        raise Exception('activations should all be tensors')


def test_vgg_freeze():
    model = build_example_model(map_to_semantic=True)
    # model = build_example_model(map_to_semantic=True)
    model_utils.freeze_vgg_module_subset(model)

    frozen_modules, unfrozen_modules = [], []
    for module_name, module in model.named_children():
        module_frozen = all([p.requires_grad is False for p in module.parameters()])
        if module_frozen:
            frozen_modules.append(module_name)
        else:
            assert all([p.requires_grad is True for p in module.parameters()])
            unfrozen_modules.append(module_name)

    non_vgg_frozen_modules = [module_name for module_name in frozen_modules
                              if module_name not in model_utils.VGG_CHILDREN_NAMES]
    vgg_frozen_modules = [module_name for module_name in frozen_modules
                          if module_name in model_utils.VGG_CHILDREN_NAMES]
    for module_name, module in model.named_children():
        if module_name in model_utils.VGG_CHILDREN_NAMES:
            assert all([p.requires_grad is False for p in module.parameters()])
    print('All modules were correctly frozen: '.format({}).format(model_utils.VGG_CHILDREN_NAMES))

    print('VGG modules frozen: {}'.format(vgg_frozen_modules))
    print('Non-VGG modules frozen: {}'.format(non_vgg_frozen_modules))
    print('Modules unfrozen: {}'.format(unfrozen_modules))
    assert set(unfrozen_modules + vgg_frozen_modules + non_vgg_frozen_modules) == \
        set([module[0] for module in model.named_children()])
    assert len([module[0] for module in model.named_children()]) == \
        len(unfrozen_modules + vgg_frozen_modules + non_vgg_frozen_modules)

    assert non_vgg_frozen_modules == ['conv1x1_instance_to_semantic'], '{}'.format(non_vgg_frozen_modules)


def test_all():
    test_forward_hook()
    test_vgg_freeze()


if __name__ == '__main__':
    test_all()
