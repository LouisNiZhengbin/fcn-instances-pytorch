import math
import os
import os.path as osp
import shutil

import numpy as np
import torch
import torch.nn.functional as F
import tqdm
from torch.autograd import Variable

import instanceseg
import instanceseg.losses.loss
import instanceseg.utils.export
import instanceseg.utils.misc
from instanceseg.train import metrics, trainer_exporter
from instanceseg.analysis import visualization_utils
from instanceseg.analysis.visualization_utils import export_visualizations
from instanceseg.datasets import runtime_transformations
from instanceseg.models.model_utils import is_nan, any_nan
from instanceseg.utils import datasets, instance_utils
from instanceseg.utils.misc import flatten_dict

DEBUG_ASSERTS = True

BINARY_AUGMENT_MULTIPLIER = 100.0
BINARY_AUGMENT_CENTERED = True


def should_write_activations(iteration, epoch, interval_validate):
    if iteration < 3000:
        return True
    else:
        return False


class Trainer(object):
    def __init__(self, cuda, model, optimizer, train_loader, val_loader, out_dir, max_iter, instance_problem,
                 size_average=True, interval_validate=None, loss_type='cross_entropy', matching_loss=True,
                 tensorboard_writer=None, train_loader_for_val=None, loader_semantic_lbl_only=False,
                 use_semantic_loss=False, augment_input_with_semantic_masks=False, export_activations=False,
                 activation_layers_to_export=(), write_activation_condition=should_write_activations,
                 write_instance_metrics=True, generate_new_synthetic_data_each_epoch=False):

        self.exporter = trainer_exporter.TrainerExporter(out_dir=out_dir, instance_problem=instance_problem,
                                                         tensorboard_writer=tensorboard_writer)
        # System parameters
        self.cuda = cuda

        # Model objects
        self.model = model

        # Training objects
        self.optim = optimizer

        # Dataset objects
        self.train_loader = train_loader
        self.val_loader = val_loader
        self.train_loader_for_val = train_loader_for_val

        # Problem setup objects
        self.instance_problem = instance_problem

        # Exporting objects
        self.tensorboard_writer = tensorboard_writer

        # Exporting parameters
        self.which_heatmaps_to_visualize = 'same semantic'  # 'all'

        # Loss parameters
        self.size_average = size_average
        self.matching_loss = matching_loss
        self.loss_type = loss_type

        # Data loading parameters
        self.loader_semantic_lbl_only = loader_semantic_lbl_only

        self.use_semantic_loss = use_semantic_loss
        self.augment_input_with_semantic_masks = augment_input_with_semantic_masks
        self.generate_new_synthetic_data_each_epoch = generate_new_synthetic_data_each_epoch

        # Writing activations
        self.export_activations = export_activations
        self.activation_layers_to_export = activation_layers_to_export
        self.write_activation_condition = write_activation_condition
        self.write_instance_metrics = write_instance_metrics

        # Stored values
        self.last_val_loss = None

        if interval_validate is None:
            self.interval_validate = len(self.train_loader)
        else:
            self.interval_validate = interval_validate

        self.out = out_dir
        if not osp.exists(self.out):
            os.makedirs(self.out)

        self.epoch = 0
        self.iteration = 0
        self.max_iter = max_iter
        self.best_mean_iu = 0
        # TODO(allie): clean up max combined class... computing accuracy shouldn't need it.
        self.n_combined_class = int(sum(self.model.semantic_instance_class_list)) + 1
        self.loss_fcn = self.build_my_loss_function()
        self.loss_fcn_matching_override = self.build_my_loss_function(matching_override=True)
        metric_maker_kwargs = {
            'problem_config': self.instance_problem,
            'component_loss_function': self.loss_fcn,
            'augment_function_img_sem': self.augment_image
            if self.augment_input_with_semantic_masks else None
        }
        self.metric_makers = {
            'val': metrics.InstanceMetrics(self.val_loader, **metric_maker_kwargs),
            'train_for_val': metrics.InstanceMetrics(self.train_loader_for_val, **metric_maker_kwargs)
        }

    def prepare_data_for_forward_pass(self, img_data, target, requires_grad=True):
        """
        Loads data and transforms it into Variable based on GPUs, input augmentations, and loader type (if semantic)
        requires_grad: True if training; False if you're not planning to backprop through (for validation / metrics)
        """
        if not self.loader_semantic_lbl_only:
            (sem_lbl, inst_lbl) = target
        else:
            assert self.use_semantic_loss, 'Can''t run instance losses if loader is semantic labels only.  Set ' \
                                           'use_semantic_loss to True'
            assert type(target) is not tuple
            sem_lbl = target
            inst_lbl = torch.zeros_like(sem_lbl)
            inst_lbl[sem_lbl == -1] = -1

        if self.cuda:
            img_data, (sem_lbl, inst_lbl) = img_data.cuda(), (sem_lbl.cuda(), inst_lbl.cuda())
        full_input = img_data if not self.augment_input_with_semantic_masks \
            else self.augment_image(img_data, sem_lbl)
        full_input, sem_lbl, inst_lbl = \
            Variable(full_input, volatile=(not requires_grad)), \
            Variable(sem_lbl, requires_grad=requires_grad), \
            Variable(inst_lbl, requires_grad=requires_grad)
        return full_input, sem_lbl, inst_lbl

    def build_my_loss_function(self, matching_override=None):
        # permutations, loss, loss_components = f(scores, sem_lbl, inst_lbl)
        matching = matching_override if matching_override is not None else self.matching_loss
        my_loss_fcn = instanceseg.losses.loss.loss_2d_factory(  # f(scores, sem_lbl, inst_lbl)
            self.loss_type, self.instance_problem.semantic_instance_class_list,
            self.instance_problem.instance_count_id_list,
            return_loss_components=True, matching=matching)
        return my_loss_fcn

    def compute_loss(self, score, sem_lbl, inst_lbl, val_matching_override=False):
        # permutations, loss, loss_components = f(scores, sem_lbl, inst_lbl)
        map_to_semantic = self.instance_problem.map_to_semantic
        if not (sem_lbl.size() == inst_lbl.size() == (score.size(0), score.size(2), score.size(3))):
            import ipdb;
            ipdb.set_trace()
            raise Exception('Sizes of score, targets are incorrect')

        if map_to_semantic:
            inst_lbl[inst_lbl > 1] = 1
        loss_fcn = self.loss_fcn if not val_matching_override else self.loss_fcn_matching_override

        permutations, loss, loss_components = loss_fcn(score, sem_lbl, inst_lbl)
        return permutations, loss, loss_components

    def augment_image(self, img, sem_lbl):
        semantic_one_hot = datasets.labels_to_one_hot(sem_lbl, self.instance_problem.n_semantic_classes)
        return datasets.augment_channels(img, BINARY_AUGMENT_MULTIPLIER * semantic_one_hot -
                                         (0.5 if BINARY_AUGMENT_CENTERED else 0), dim=1)

    def validate(self, split='val', write_basic_metrics=None, write_instance_metrics=None, save_checkpoint=None,
                 update_best_checkpoint=None, should_export_visualizations=True):
        """
        If split == 'val': write_metrics, save_checkpoint, update_best_checkpoint default to True.
        If split == 'train': write_metrics, save_checkpoint, update_best_checkpoint default to
            False.
        """
        val_metrics = None
        write_instance_metrics = (split == 'val') and self.write_instance_metrics \
            if write_instance_metrics is None else write_instance_metrics
        write_basic_metrics = True if write_basic_metrics is None else write_basic_metrics
        save_checkpoint = (split == 'val') if save_checkpoint is None else save_checkpoint
        update_best_checkpoint = save_checkpoint if update_best_checkpoint is None \
            else update_best_checkpoint
        should_compute_basic_metrics = \
            write_basic_metrics or write_instance_metrics or save_checkpoint or update_best_checkpoint

        assert split in ['train', 'val']
        if split == 'train':
            data_loader = self.train_loader_for_val
        else:
            data_loader = self.val_loader

        # eval instead of training mode temporarily
        training = self.model.training
        self.model.eval()

        val_loss = 0
        segmentation_visualizations, score_visualizations = [], []
        label_trues, label_preds, scores, pred_permutations = [], [], [], []
        visualizations_need_to_be_exported = True if should_export_visualizations else False
        num_images_to_visualize = min(len(data_loader), 9)
        for batch_idx, (img_data, lbls) in tqdm.tqdm(
                enumerate(data_loader), total=len(data_loader),
                desc='Valid iteration (split=%s)=%d' % (split, self.iteration), ncols=80,
                leave=False):

            should_visualize = len(segmentation_visualizations) < num_images_to_visualize
            if not (should_compute_basic_metrics or should_visualize):
                # Don't waste computation if we don't need to run on the remaining images
                continue
            true_labels_sb, pred_labels_sb, score_sb, pred_permutations_sb, val_loss_sb, \
            segmentation_visualizations_sb, score_visualizations_sb = \
                self.validate_single_batch(img_data, lbls[0], lbls[1], data_loader=data_loader,
                                           should_visualize=should_visualize)
            if visualizations_need_to_be_exported and len(segmentation_visualizations) == num_images_to_visualize:
                self.export_visualizations(segmentation_visualizations, 'seg_' + split, tile=True)
                self.export_visualizations(score_visualizations, 'score_' + split, tile=False)
                visualizations_need_to_be_exported = False

            label_trues += true_labels_sb
            label_preds += pred_labels_sb
            val_loss += val_loss_sb
            scores += [score_sb]
            pred_permutations += [pred_permutations_sb]
            segmentation_visualizations += segmentation_visualizations_sb
            score_visualizations += score_visualizations_sb

        if visualizations_need_to_be_exported and len(segmentation_visualizations) == num_images_to_visualize:
            if should_export_visualizations:
                self.export_visualizations(segmentation_visualizations, 'seg_' + split, tile=True)
                self.export_visualizations(score_visualizations, 'score_' + split, tile=False)

        val_loss /= len(data_loader)
        self.last_val_loss = val_loss

        if should_compute_basic_metrics:
            val_metrics = self.compute_metrics(label_trues, label_preds, pred_permutations)
            if write_basic_metrics:
                self.exporter.write_metrics(val_metrics, val_loss, split, epoch=self.epoch, iteration=self.iteration)
                if self.tensorboard_writer is not None:
                    self.tensorboard_writer.add_scalar('metrics/{}/losses'.format(split),
                                                       val_loss, self.iteration)
                    self.tensorboard_writer.add_scalar('metrics/{}/mIOU'.format(split), val_metrics[2],
                                                       self.iteration)

            if save_checkpoint:
                self.save_checkpoint()
            if update_best_checkpoint:
                self.update_best_checkpoint_if_best(mean_iu=val_metrics[2])
        if write_instance_metrics:
            self.compute_and_write_instance_metrics()

        # Restore training settings set prior to function call
        if training:
            self.model.train()

        visualizations = (segmentation_visualizations, score_visualizations)
        return val_metrics, visualizations

    def retrieve_and_write_batch_activations(self, batch_input):
        if self.tensorboard_writer is not None:
            activations = self.model.get_activations(batch_input, self.activation_layers_to_export)
            histogram_activations = activations
            for name, activations in tqdm.tqdm(histogram_activations.items(),
                                               total=len(histogram_activations.items()),
                                               desc='Writing activation distributions', leave=False):
                if name == 'upscore8':
                    channel_labels = self.instance_problem.get_model_channel_labels('{}_{}')
                    assert activations.size(1) == len(channel_labels), '{} != {}'.format(activations.size(1),
                                                                                         len(channel_labels))
                    for c, channel_label in enumerate(channel_labels):
                        self.tensorboard_writer.add_histogram('batch_activations/{}/{}'.format(name, channel_label),
                                                              activations[:, c, :, :].cpu().numpy(),
                                                              self.iteration, bins='auto')
                elif name == 'conv1x1_instance_to_semantic':
                    channel_labels = self.instance_problem.get_channel_labels('{}_{}')
                    assert activations.size(1) == len(channel_labels)
                    for c, channel_label in enumerate(channel_labels):
                        try:
                            self.tensorboard_writer.add_histogram('batch_activations/{}/{}'.format(name, channel_label),
                                                                  activations[:, c, :, :].cpu().numpy(),
                                                                  self.iteration, bins='auto')
                        except IndexError as ie:
                            print('WARNING: Didn\'t write activations.  IndexError: {}'.format(ie))
                elif name == 'conv1_1':
                    # This is expensive to write, so we'll just write a representative set.
                    min = torch.min(activations)
                    max = torch.max(activations)
                    mean = torch.mean(activations)
                    representative_set = np.ndarray((100, 3))
                    representative_set[:, 0] = min
                    representative_set[:, 1] = max
                    representative_set[:, 2] = mean
                    self.tensorboard_writer.add_histogram('batch_activations/{}/min_mean_max_all_channels'.format(name),
                                                          representative_set, self.iteration, bins='auto')
                    continue

                self.tensorboard_writer.add_histogram('batch_activations/{}/all_channels'.format(name),
                                                      activations.cpu().numpy(), self.iteration, bins='auto')

    def compute_and_write_instance_metrics(self):
        if self.tensorboard_writer is not None:
            for split, metric_maker in tqdm.tqdm(self.metric_makers.items(), desc='Computing instance metrics',
                                                 total=len(self.metric_makers.items()), leave=False):
                metric_maker.clear()
                metric_maker.compute_metrics(self.model)
                metrics_as_nested_dict = metric_maker.get_aggregated_scalar_metrics_as_nested_dict()
                metrics_as_flattened_dict = flatten_dict(metrics_as_nested_dict)
                for name, metric in metrics_as_flattened_dict.items():
                    self.tensorboard_writer.add_scalar('instance_metrics_{}/{}'.format(split, name), metric,
                                                       self.iteration)
                histogram_metrics_as_nested_dict = metric_maker.get_aggregated_histogram_metrics_as_nested_dict()
                histogram_metrics_as_flattened_dict = flatten_dict(histogram_metrics_as_nested_dict)
                if self.iteration != 0:  # screws up the axes if we do it on the first iteration with weird inits
                    # if 1:
                    for name, metric in tqdm.tqdm(histogram_metrics_as_flattened_dict.items(),
                                                  total=len(histogram_metrics_as_flattened_dict.items()),
                                                  desc='Writing histogram metrics', leave=False):
                        if torch.is_tensor(metric):
                            self.tensorboard_writer.add_histogram('instance_metrics_{}/{}'.format(split, name),
                                                                  metric.numpy(), self.iteration, bins='auto')
                        elif isinstance(metric, np.ndarray):
                            self.tensorboard_writer.add_histogram('instance_metrics_{}/{}'.format(split, name), metric,
                                                                  self.iteration, bins='auto')
                        elif metric is None:
                            import ipdb;
                            ipdb.set_trace()
                            pass
                        else:
                            import ipdb;
                            ipdb.set_trace()
                            raise ValueError('I\'m not sure how to write {} to tensorboard_writer (name is '
                                             ' '.format(type(metric), name))

    def compute_metrics(self, label_trues, label_preds, permutations=None, single_batch=False):
        if permutations is not None:
            if single_batch:
                permutations = [permutations]
            assert type(permutations) == list, \
                NotImplementedError('I''m assuming permutations are a list of ndarrays from multiple batches, '
                                    'not type {}'.format(type(permutations)))
            label_preds_permuted = [instance_utils.permute_labels(label_pred, perms)
                                    for label_pred, perms in zip(label_preds, permutations)]
        else:
            label_preds_permuted = label_preds
        metrics_list = instanceseg.utils.misc.label_accuracy_score(label_trues, label_preds_permuted,
                                                                   n_class=self.n_combined_class)
        return metrics_list

    def export_visualizations(self, visualizations, basename='val_', tile=True, outdir=None):
        outdir = outdir or osp.join(self.out, 'visualization_viz')
        export_visualizations(visualizations, outdir, self.tensorboard_writer, self.iteration, basename=basename,
                              tile=tile)

    def save_checkpoint(self):
        torch.save({
            'epoch': self.epoch,
            'iteration': self.iteration,
            'arch': self.model.__class__.__name__,
            'optim_state_dict': self.optim.state_dict(),
            'model_state_dict': self.model.state_dict(),
            'best_mean_iu': self.best_mean_iu,
        }, osp.join(self.out, 'checkpoint.pth.tar'))

    def update_best_checkpoint_if_best(self, mean_iu):
        is_best = mean_iu > self.best_mean_iu
        if is_best:
            self.best_mean_iu = mean_iu
            shutil.copy(osp.join(self.out, 'checkpoint.pth.tar'),
                        osp.join(self.out, 'model_best.pth.tar'))

    def validate_single_batch(self, img_data, sem_lbl, inst_lbl, data_loader, should_visualize):
        true_labels = []
        pred_labels = []
        pred_permutations = []
        segmentation_visualizations = []
        score_visualizations = []
        val_loss = 0

        full_input, sem_lbl, inst_lbl = self.prepare_data_for_forward_pass(img_data, (sem_lbl, inst_lbl),
                                                                           requires_grad=False)

        imgs = img_data.cpu()

        score = self.model(full_input)
        pred_permutations, loss, _ = self.compute_loss(score, sem_lbl, inst_lbl, val_matching_override=True)
        if is_nan(loss.data[0]):
            raise ValueError('losses is nan while validating')
        val_loss += float(loss.data[0]) / len(full_input)

        softmax_scores = F.softmax(score, dim=1).data.cpu().numpy()
        inst_lbl_pred = score.data.max(dim=1)[1].cpu().numpy()[:, :, :]

        # TODO(allie): convert to sem, inst visualizations.
        lbl_true_sem, lbl_true_inst = (sem_lbl.data.cpu(), inst_lbl.data.cpu())
        if DEBUG_ASSERTS:
            assert inst_lbl_pred.shape == lbl_true_inst.shape
        for idx, (img, sem_lbl, inst_lbl, lp) in enumerate(zip(imgs, lbl_true_sem, lbl_true_inst, inst_lbl_pred)):
            # runtime_transformation needs to still run the resize, even for untransformed img, lbl pair
            if data_loader.dataset.runtime_transformation is not None:
                runtime_transformation_undo = runtime_transformations.GenericSequenceRuntimeDatasetTransformer(
                    [t for t in (data_loader.dataset.runtime_transformation.transformer_sequence or [])
                     if isinstance(t, runtime_transformations.BasicRuntimeDatasetTransformer)])
                img_untransformed, lbl_untransformed = runtime_transformation_undo.untransform(img, (sem_lbl, inst_lbl))

            sem_lbl_np = lbl_untransformed[0]
            inst_lbl_np = lbl_untransformed[1]

            pp = pred_permutations[idx, :]
            lt_combined = self.gt_tuple_to_combined(sem_lbl_np, inst_lbl_np)
            true_labels.append(lt_combined)
            pred_labels.append(lp)
            if should_visualize:
                # Segmentations

                viz = visualization_utils.visualize_segmentation(
                    lbl_pred=lp, lbl_true=lt_combined, pred_permutations=pp, img=img_untransformed,
                    n_class=self.n_combined_class, overlay=False)
                segmentation_visualizations.append(viz)
                # Scores
                sp = softmax_scores[idx, :, :, :]

                # TODO(allie): Fix this -- bug(?!)
                lp = np.argmax(sp, axis=0)
                # try:
                #     assert np.all(np.argmax(sp, axis=0) == lp)
                # except:
                #     import ipdb; ipdb.set_trace()
                if self.which_heatmaps_to_visualize == 'same semantic':
                    inst_sem_classes_present = torch.np.unique(true_labels)
                    inst_sem_classes_present = inst_sem_classes_present[inst_sem_classes_present != -1]
                    sem_classes_present = np.unique([self.instance_problem.semantic_instance_class_list[c]
                                                     for c in inst_sem_classes_present])
                    channels_for_these_semantic_classes = [inst_idx for inst_idx, sem_cls in enumerate(
                        self.instance_problem.semantic_instance_class_list) if sem_cls in sem_classes_present]
                    channels_to_visualize = channels_for_these_semantic_classes
                elif self.which_heatmaps_to_visualize == 'all':
                    channels_to_visualize = list(range(sp.shape[0]))
                else:
                    raise ValueError('which heatmaps to visualize is not recognized: {}'.format(
                        self.which_heatmaps_to_visualize))
                channel_labels = self.instance_problem.get_channel_labels('{} {}')
                viz = visualization_utils.visualize_heatmaps(scores=sp,
                                                             lbl_true=lt_combined,
                                                             lbl_pred=lp,
                                                             pred_permutations=pp,
                                                             n_class=self.n_combined_class,
                                                             score_vis_normalizer=sp.max(),
                                                             channel_labels=channel_labels,
                                                             channels_to_visualize=channels_to_visualize,
                                                             input_image=img_untransformed)
                score_visualizations.append(viz)
        return true_labels, pred_labels, score, pred_permutations, val_loss, segmentation_visualizations, \
               score_visualizations

    def gt_tuple_to_combined(self, sem_lbl, inst_lbl):
        semantic_instance_class_list = self.instance_problem.semantic_instance_class_list
        instance_count_id_list = self.instance_problem.instance_count_id_list
        return instance_utils.combine_semantic_and_instance_labels(sem_lbl, inst_lbl,
                                                                   semantic_instance_class_list,
                                                                   instance_count_id_list)

    def train_epoch(self):
        self.model.train()
        last_loss = None

        if self.generate_new_synthetic_data_each_epoch:
            seed = np.random.randint(100)
            self.train_loader.dataset.raw_dataset.initialize_locations_per_image(seed)
            self.train_loader_for_val.dataset.raw_dataset.initialize_locations_per_image(seed)

        for batch_idx, (img_data, target) in tqdm.tqdm(  # tqdm: progress bar
                enumerate(self.train_loader), total=len(self.train_loader),
                desc='Train epoch=%d' % self.epoch, ncols=80, leave=False):
            iteration = batch_idx + self.epoch * len(self.train_loader)
            if self.iteration != 0 and (iteration - 1) != self.iteration:
                continue  # for resuming
            self.iteration = iteration
            if self.iteration % self.interval_validate == 0:
                val_metrics, _ = self.validate()
                val_loss = self.last_val_loss
                if self.train_loader_for_val is not None:
                    train_metrics, _ = self.validate('train')
                    train_loss = self.last_val_loss
                else:
                    print('Warning: cannot generate train vs. val plots if we dont have access to the training losses '
                          'via train_for_val dataloader')
                    train_loss = None
                if train_loss is not None:
                    self.exporter.update_mpl_joint_train_val_loss_figure(train_loss, val_loss, iteration)
                    if self.tensorboard_writer is not None:
                        self.tensorboard_writer.add_scalar('val_minus_train_loss', val_loss - train_loss,
                                                           self.iteration)

            assert self.model.training
            full_input, sem_lbl, inst_lbl = self.prepare_data_for_forward_pass(img_data, target, requires_grad=True)
            self.optim.zero_grad()

            score = self.model(full_input)
            pred_permutations, loss, _ = self.compute_loss(score, sem_lbl, inst_lbl)
            if is_nan(loss.data[0]):
                import ipdb;
                ipdb.set_trace()
                raise ValueError('losses is nan while training')
            loss /= len(full_input)
            if loss.data[0] > 1e4:
                print('WARNING: losses={} at iteration {}'.format(loss.data[0], self.iteration))
            if any_nan(score.data):
                import ipdb;
                ipdb.set_trace()
                raise ValueError('score is nan while training')
            if self.tensorboard_writer is not None:
                self.tensorboard_writer.add_scalar('metrics/train_batch_loss', loss.data[0],
                                                   self.iteration)
            loss.backward()
            self.optim.step()

            inst_lbl_pred = score.data.max(1)[1].cpu().numpy()[:, :, :]
            lbl_true_sem, lbl_true_inst = sem_lbl.data.cpu().numpy(), inst_lbl.data.cpu().numpy()
            metrics = []
            for sem_lbl_np, inst_lbl_np, lp in zip(lbl_true_sem, lbl_true_inst, inst_lbl_pred):
                lt_combined = self.gt_tuple_to_combined(sem_lbl_np, inst_lbl_np)
                acc, acc_cls, mean_iu, fwavacc = \
                    self.compute_metrics(label_trues=[lt_combined], label_preds=[lp], permutations=[pred_permutations])
                metrics.append((acc, acc_cls, mean_iu, fwavacc))
            metrics = np.mean(metrics, axis=0)
            self.exporter.write_metrics(metrics, loss, split='train', epoch=self.epoch, iteration=self.iteration)
            if self.iteration >= self.max_iter:
                break

            if self.tensorboard_writer is not None:
                self.model.eval()
                new_score = self.model(full_input)
                if any_nan(new_score.data):
                    import ipdb;
                    ipdb.set_trace()
                    raise ValueError('new_score became nan while training')
                new_pred_permutations, new_loss, _ = self.compute_loss(new_score, sem_lbl, inst_lbl)
                new_loss /= len(full_input)
                loss_improvement = loss.data[0] - new_loss.data[0]
                self.model.train()

                self.tensorboard_writer.add_scalar('metrics/train_batch_loss_improvement', loss_improvement,
                                                   self.iteration)
                self.tensorboard_writer.add_scalar('metrics/reassignment',
                                                   np.sum(new_pred_permutations != pred_permutations),
                                                   self.iteration)
                if self.export_activations and self.write_activation_condition(iteration=self.iteration, \
                                                                               epoch=self.epoch,
                                                                               interval_validate=self.interval_validate):
                    self.retrieve_and_write_batch_activations(full_input)
                if is_nan(new_loss.data[0]):
                    import ipdb;
                    ipdb.set_trace()
                    raise ValueError('new_loss is nan while training')
            last_loss = loss.data.clone()

    def train(self):
        max_epoch = int(math.ceil(1. * self.max_iter / len(self.train_loader)))
        for epoch in tqdm.trange(self.epoch, max_epoch,
                                 desc='Train', ncols=80, leave=True):
            self.epoch = epoch
            self.train_epoch()
            if self.iteration >= self.max_iter:
                break