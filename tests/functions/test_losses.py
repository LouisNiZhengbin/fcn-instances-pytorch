import os.path as osp

from instanceseg.losses.match import GT_VALUE_FOR_FALSE_POSITIVE
from instanceseg.utils import parse
from instanceseg.utils.script_setup import setup_train, configure
import torch

here = osp.dirname(osp.abspath(__file__))


def get_single_img_data(dataloader, idx=0):
    img, sem_lbl, inst_lbl = None, None, None
    for i, (img, (sem_lbl, inst_lbl)) in enumerate(dataloader):
        if i != idx:
            continue
    return img, (sem_lbl, inst_lbl)


def main():
    args, cfg_override_args = parse.parse_args_without_sys(dataset_name='synthetic')
    cfg_override_args.loss_type = 'soft_iou'
    cfg_override_args.size_average = False
    cfg, out_dir, sampler_cfg = configure(dataset_name=args.dataset,
                                          config_idx=args.config,
                                          sampler_name=args.sampler,
                                          script_py_file=__file__,
                                          cfg_override_args=cfg_override_args)
    trainer = setup_train(args.dataset, cfg, out_dir, sampler_cfg, gpu=args.gpu, checkpoint_path=args.resume,
                          semantic_init=args.semantic_init)

    img_data, (sem_lbl, inst_lbl) = get_single_img_data(trainer.dataloaders['train'], idx=0)
    full_input, sem_lbl, inst_lbl = trainer.prepare_data_for_forward_pass(img_data, (sem_lbl, inst_lbl),
                                                                          requires_grad=False)
    score_1 = trainer.model(full_input)
    score_gt = score_1.clone()
    score_gt[...] = 0
    magnitude_gt = 100
    for c in range(score_1.size(1)):
        score_gt[:, c, :, :] = (sem_lbl == trainer.instance_problem.model_channel_semantic_ids[c]).float() * \
                               (inst_lbl == trainer.instance_problem.instance_count_id_list[c]).float() * magnitude_gt
    magnitude_1 = 1
    for c in range(score_1.size(1)):
        import numpy as np
        inst_vals = range(1, max(trainer.instance_problem.instance_count_id_list) + 1)
        permuted = np.random.permutation(inst_vals)
        inst_to_permuted = {0: 0}
        inst_to_permuted.update({
            i: p for i, p in zip(inst_vals, permuted)
        })
        score_1[:, c, :, :] = (sem_lbl == trainer.instance_problem.model_channel_semantic_ids[c]).float() * \
                              (inst_lbl == inst_to_permuted[trainer.instance_problem.instance_count_id_list[
                                   c]]).float() * magnitude_1
    try:
        assert (score_gt.sum(dim=1) == magnitude_gt).all()  # debug sanity check
    except AssertionError:
        import ipdb
        ipdb.set_trace()
    loss_object = trainer.loss_object
    score_1_copy = score_1.clone()
    # cost_matrix_gt = loss_object.build_all_sem_cls_cost_matrices_as_tensor_data(
    #     loss_object.transform_scores_to_predictions(score_gt)[0, ...], sem_lbl[0, ...], inst_lbl[0, ...])
    loss_result = trainer.compute_loss(score_gt, sem_lbl, inst_lbl)
    assignments_gt, avg_loss_gt, loss_components_gt = \
        loss_result.assignments, loss_result.avg_loss, loss_result.loss_components_by_channel
    loss_result = trainer.compute_loss(score_1, sem_lbl, inst_lbl)
    assignments_1, avg_loss_1, loss_components_1 = \
        loss_result.assignments, loss_result.avg_loss, loss_result.loss_components_by_channel

    assigned_gt_inst_vals = assignments_1.assigned_gt_inst_vals
    sem_vals = assignments_1.sem_values
    model_channel_sem_vals = trainer.instance_problem.model_channel_semantic_ids
    model_channel_inst_vals = trainer.instance_problem.instance_count_id_list
    permuted_score_1 = permute_predictions_to_match_gt_inst_vals(assigned_gt_inst_vals, assignments_1,
                                                                 model_channel_inst_vals,
                                                                 model_channel_sem_vals, score_1_copy, sem_vals)
    permuted_score_100 = permuted_score_1.clone() * magnitude_gt

    loss_object.matching = False
    # loss_res = loss_object.loss_fcn(permuted_score_1, sem_lbl, inst_lbl)
    # assignments_perm1, avg_loss_perm1, loss_components_perm1 = \
    #     loss_res['assignments'], loss_res['total_loss'], loss_res['loss_components_by_channel']
    # loss_res = loss_object.loss_fcn(permuted_score_100, sem_lbl, inst_lbl)
    # assignments_perm100, avg_loss_perm100, loss_components_perm100 = \
    #     loss_res['assignments'], loss_res['total_loss'], loss_res['loss_components_by_channel']

    import ipdb; ipdb.set_trace()


def permute_predictions_to_match_gt_inst_vals(assigned_gt_inst_vals, assignments_1, model_channel_inst_vals,
                                              model_channel_sem_vals, score_1, sem_vals):
    permuted_score_1 = score_1.clone() * float('nan')
    for data_idx in range(score_1.shape[0]):
        assert all(x1 == x2 for x1, x2 in zip(assignments_1.model_channels[data_idx, :], range(score_1.size(1))))
        unassigned_val = -10
        new_channels = unassigned_val * torch.ones((score_1.size(1)), dtype=torch.long)
        for sem_val in torch.unique(sem_vals[data_idx, :]):
            non_FP_channels = [i for i, (iv, sv) in enumerate(zip(assigned_gt_inst_vals[data_idx, :],
                                                                  sem_vals[data_idx, :]))
                               if sv == sem_val and (iv != GT_VALUE_FOR_FALSE_POSITIVE)]
            FP_channels = [i for i, (iv, sv) in enumerate(zip(assigned_gt_inst_vals[data_idx, :],
                                                              sem_vals[data_idx, :]))
                           if sv == sem_val and (iv == GT_VALUE_FOR_FALSE_POSITIVE)]
            for c in non_FP_channels:
                gt_inst_val = assigned_gt_inst_vals[data_idx, c]
                corresponding_inst_val_channel = [i for i, (sv, iv) in
                                                  enumerate(zip(model_channel_sem_vals,
                                                                model_channel_inst_vals)) if sv == sem_val and
                                                  iv == gt_inst_val]
                assert len(corresponding_inst_val_channel) == 1
                corresponding_inst_val_channel = corresponding_inst_val_channel[0]
                new_channels[c] = corresponding_inst_val_channel
            unassigned_channels = [i for i, (sv, new_c) in
                                   enumerate(zip(model_channel_sem_vals,
                                                 new_channels)) if sv == sem_val and
                                   new_c == unassigned_val]
            assert len(unassigned_channels) == len(FP_channels)
            import ipdb; ipdb.set_trace()
            for c, new_c in zip(FP_channels, unassigned_channels):
                assert assigned_gt_inst_vals[data_idx, new_c] == GT_VALUE_FOR_FALSE_POSITIVE
                new_channels[c] = new_c
        assert len(torch.unique(new_channels)) == new_channels.numel()  # all must be unique
        for c, new_c in enumerate(new_channels):
            permuted_score_1[data_idx, new_c, :, :] = score_1[data_idx, c, :, :]
    assert torch.all(~torch.isnan(permuted_score_1))
    return permuted_score_1


if __name__ == '__main__':
    main()
