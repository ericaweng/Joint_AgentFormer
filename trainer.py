import os
import time
from itertools import starmap
from functools import partial
import multiprocessing
import numpy as np
import torch
import pytorch_lightning as pl

from model.model_lib import model_dict
from eval import eval_one_seq2
from metrics import stats_func
from utils.utils import mkdir_if_missing
from utils.torch import get_scheduler
# from viz_utils import get_metrics_str, plot_anim_grid
from viz_utils import get_metrics_str
from visualization_scripts.viz_utils2 import plot_anim_grid
from visualization_scripts.viz_utils_3d import AnimObjPose
from collision_rejection import run_model_w_col_rej


def save_trajectories(trajectory, save_dir, seq_name, frame, suffix=''):
    """Save trajectories in a text file.
    Input:
        trajectory: (np.array/torch.Tensor) Predcited trajectories with shape
                    of (n_pedestrian, future_timesteps, 4). The last elemen is
                    [frame_id, track_id, x, y] where each element is float.
        save_dir: (str) Directory to save into.
        seq_name: (str) Sequence name (e.g., eth_biwi, coupa_0)
        frame: (num) Frame ID.
        suffix: (str) Additional suffix to put into file name.
    """
    fname = f"{save_dir}/{seq_name}/frame_{int(frame):06d}{suffix}.txt"
    mkdir_if_missing(fname)

    if isinstance(trajectory, torch.Tensor):
        trajectory = trajectory.cpu().numpy()
    np.savetxt(fname, trajectory, fmt="%.3f")


def format_agentformer_trajectories(trajectory, data, cfg, timesteps=12, frame_scale=10, future=True):
    formatted_trajectories = []
    if not future:
        trajectory = torch.flip(trajectory, [0, 1])
    for i, track_id in enumerate(data['valid_id']):
        if data['pred_mask'] is not None and data['pred_mask'][i] != 1.0:
            continue
        for j in range(timesteps):
            if future:
                curr_data = data['fut_data'][j]
            else:
                curr_data = data['pre_data'][j]
            # Get data with the same track_id
            updated_data = curr_data[curr_data[:, 1] == track_id].squeeze()
            if cfg.dataset in [
                    'eth', 'hotel', 'univ', 'zara1', 'zara2', 'gen',
                    'real_gen', 'adversarial'
            ]:
                # [13, 15] correspoinds to the 2D position
                updated_data[[13, 15]] = trajectory[i, j].cpu().numpy()
            elif 'sdd' in cfg.dataset:
                updated_data[[2, 3]] = trajectory[i, j].cpu().numpy()
            else:
                raise NotImplementedError()
            formatted_trajectories.append(updated_data)
    if len(formatted_trajectories) == 0:
        return np.array([])

    # Convert to numpy array and get [frame_id, track_id, x, y]
    formatted_trajectories = np.vstack(formatted_trajectories)
    if cfg.dataset in [ 'eth', 'hotel', 'univ', 'zara1', 'zara2' ]:
        formatted_trajectories = formatted_trajectories[:, [0, 1, 13, 15]]
        formatted_trajectories[:, 0] *= frame_scale
    elif cfg.dataset == 'trajnet_sdd':
        formatted_trajectories[:, 0] *= frame_scale

    if not future:
        formatted_trajectories = np.flip(formatted_trajectories, axis=0)

    return formatted_trajectories


class AgentFormerTrainer(pl.LightningModule):
    def __init__(self, cfg, args):
        super().__init__()
        model_id = cfg.get('model_id', 'agentformer')
        self.model = model_dict[model_id](cfg)
        self.cfg = cfg
        self.args = args
        num_workers = int(multiprocessing.cpu_count() / (args.devices + 1e-5)) if args.devices is not None else float('inf')
        self.num_workers = min(args.num_workers, num_workers)
        self.batch_size = args.batch_size
        self.collision_rad = cfg.get('collision_rad', 0.1)
        self.hparams.update(vars(cfg))
        self.hparams.update(vars(args))
        self.model_name = "_".join(self.cfg.id.split("_")[1:])
        self.dataset_name = self.cfg.id.split("_")[0].replace('-', '_')

    def update_args(self, args):
        self.args = args

    def on_test_start(self):
        self.start = time.time()
        self.model.set_device(self.device)

    def on_fit_start(self):
        self.model.set_device(self.device)

    def _step(self, batch, mode):
        # Compute predictions
        # data = self(batch)
        self.model.set_data(batch)
        data = self.model()
        total_loss, loss_dict, loss_unweighted_dict = self.model.compute_loss()

        # losses
        self.log(f'{mode}/loss', total_loss, on_epoch=True, sync_dist=True, logger=True, batch_size=self.batch_size)
        for loss_name, loss in loss_dict.items():
            self.log(f'{mode}/{loss_name}', loss, on_step=False, on_epoch=True, sync_dist=True, logger=True, batch_size=self.batch_size)

        gt_motion = self.cfg.traj_scale * data['fut_motion'].transpose(1, 0).cpu()
        pred_motion = self.cfg.traj_scale * data[f'infer_dec_motion'].detach().cpu()
        obs_motion = self.cfg.traj_scale * data[f'pre_motion'].cpu()  # .transpose(1, 0).cpu()
        return {'loss': total_loss, **loss_dict, 'frame': batch['frame'], 'seq': batch['seq'],
                'gt_motion': gt_motion, 'pred_motion': pred_motion, 'obs_motion': obs_motion, 'data': data}

    def training_step(self, batch, batch_idx):
        if self.args.tqdm_rate == 0 and batch_idx % 5 == 0:
            print(f"epoch: {self.current_epoch} batch: {batch_idx}")
        return self._step(batch, 'train')

    def validation_step(self, batch, batch_idx):
        return self._step(batch, 'val')

    def test_step(self, batch, batch_idx):
        if self.cfg.get('collisions_ok', True):
            return_dict = self._step(batch, 'test')
        else:
            return_dict = run_model_w_col_rej(batch, self.model, self.cfg.traj_scale, self.cfg.sample_k,
                                              self.cfg.collision_rad, self.model.device)
        pred_motion = return_dict['pred_motion']
        gt_motion = return_dict['gt_motion']
        obs_motion = return_dict['obs_motion']

        if self.args.save_traj:
            if self.dataset_name == 'trajnet_sdd':
                save_dir = f'../trajectory_reward/results/trajectories/{self.model_name}/trajnet_sdd'
            else:
                save_dir = f'../trajectory_reward/results/trajectories/{self.model_name}'
            frame = batch['frame'] * batch['frame_scale']
            for idx, sample in enumerate(pred_motion.transpose(0, 1)):
                formatted = format_agentformer_trajectories(sample, batch, self.cfg, timesteps=12,
                                                            frame_scale=batch['frame_scale'], future=True)
                save_trajectories(formatted, save_dir, batch['seq'], frame, suffix=f"/sample_{idx:03d}")
            formatted = format_agentformer_trajectories(gt_motion, batch, self.cfg, timesteps=12,
                                                        frame_scale=batch['frame_scale'], future=True)
            save_trajectories(formatted, save_dir, batch['seq'], frame, suffix='/gt')
            formatted = format_agentformer_trajectories(obs_motion.transpose(0, 1), batch, self.cfg, timesteps=8,
                                                        frame_scale=batch['frame_scale'], future=False)
            save_trajectories(formatted, save_dir, batch['seq'], frame, suffix="/obs")

        return return_dict

    def _epoch_end(self, outputs, mode='test'):
        # dt = time.time() - self.start
        # print(f"Time taken for {mode} epoch: {dt:.2f} seconds")

        args_list = [(output['pred_motion'].numpy(), output['gt_motion'].numpy()) for output in outputs]

        # calculate metrics for each sequence
        if self.args.mp:
            with multiprocessing.Pool(self.num_workers) as pool:
                all_metrics = pool.starmap(partial(eval_one_seq2,
                                                   collision_rad=self.collision_rad,
                                                   return_sample_vals=self.args.save_viz), args_list)
        else:
            all_metrics = starmap(partial(eval_one_seq2,
                                          collision_rad=self.collision_rad,
                                          return_sample_vals=self.args.save_viz), args_list)
        all_metrics, all_sample_vals, argmins, collision_mats = zip(*all_metrics)

        # aggregate metrics across sequences
        num_agent_per_seq = np.array([output['gt_motion'].shape[0] for output in outputs])
        total_num_agents = np.sum(num_agent_per_seq)
        results_dict = {}
        for key, values in zip(stats_func.keys(), zip(*all_metrics)):
            if '_joint' in key or 'CR' in key:  # sequence-based metric
                value = np.mean(values)
            else:  # agent-based metric
                value = np.sum(values * num_agent_per_seq) / np.sum(num_agent_per_seq)
            results_dict[key] = value

        # get stats related to collision_rejection sampling
        is_test_mode = mode == 'test'
        if not self.cfg.get('collisions_ok', True) and is_test_mode:
            tot_samples_w_col = np.sum([0 if output['num_samples_w_col'] is None
                                        else output['num_samples_w_col'][1] for output in outputs])
            tot_frames_w_col = np.sum([0 if output['num_samples_w_col'] is None else 1 for output in outputs])
            results_dict['tot_samples_w_col'] = tot_samples_w_col
            results_dict['tot_frames_w_col'] = tot_frames_w_col

        # log and print results
        test_results_filename = f'../trajectory_reward/results/trajectories/test_results/{self.args.cfg}.tsv'
        mkdir_if_missing(test_results_filename)

        # save results to file
        if is_test_mode and self.args.save_test_results and not self.args.trial:
            with open(test_results_filename, 'w') as f:
                with open(os.path.join(self.args.default_root_dir, f'test_results.tsv'), 'w') as g:
                    f.write(f"epoch\t{self.current_epoch}\n")
                    g.write(f"epoch\t{self.current_epoch}\n")
                    metrics_to_print = {'ADE_marginal', 'FDE_marginal', 'CR_mean', 'ADE_joint', 'FDE_joint'}
                    for key, value in results_dict.items():
                        if key not in metrics_to_print:
                            continue
                        f.write(f"{key}\t{value:.4f}\n")
                        g.write(f"{key}\t{value:.4f}\n")
                    f.write(f"total_peds\t{total_num_agents}")
                    g.write(f"total_peds\t{total_num_agents}")
            print(f"wrote test results to {test_results_filename}")

        # save the frame numbers of the scenes with collisions, label with the number of samples with collisions
        if not self.cfg.get('collisions_ok', True) and is_test_mode:
            idxs_to_plot = [i for i, output in enumerate(outputs) if output['num_samples_w_col'] is not None]
            # save the frame numbers of the scenes with collisions, label with the number of samples with collisions
            frames = np.array([[outputs[i]['seq'], outputs[i]['frame'], *outputs[i]['num_samples_w_col']] for i in idxs_to_plot])
            collision_failure_stats_filename = os.path.join(self.args.logs_root, 'test_results', f'colliding_frame_nums_{self.args.cfg}.tsv')
            mkdir_if_missing(test_results_filename)
            np.savetxt(collision_failure_stats_filename, frames, fmt='%s')

        # print results to console for easy copy-and-paste
        if is_test_mode:
            print(f"\n\n\n{self.current_epoch}")
            for key, value in results_dict.items():
                print(f"{value:.4f}")
            print(total_num_agents)

        # log metrics to tensorboard
        for key, value in results_dict.items():
            self.log(f'{mode}/{key}', value, sync_dist=True, prog_bar=True, logger=True)
        self.log(f'{mode}/total_num_agents', float(total_num_agents), sync_dist=True, logger=True)

        # plot visualizations if there are collisions; or if args.save_viz and in test_mode
        if self.args.save_viz:
            num_test_samples = len(outputs)
            skip = max(1, int(num_test_samples / self.args.save_num))
            all_figs = self._save_viz(outputs[::skip], all_sample_vals[::skip], all_metrics[::skip], argmins[::skip], collision_mats[::skip], mode)

            # plot videos to tensorboard
            instance_is = np.arange(0, num_test_samples, skip)
            for idx, (instance_i, figs) in enumerate(zip(instance_is, all_figs)):
                video_tensor = np.stack(all_figs).transpose(0, 1, 4, 2, 3)
                # self.logger.experiment.add_video(f'{mode}/traj', video_tensor, self.global_step, fps=6)
                self.logger.experiment.add_video(f'{mode}/traj_{instance_i}', video_tensor[idx:idx+1], self.global_step, fps=6)


    def _save_viz_w_pose(self, outputs, all_sample_vals, all_meters_values, argmins, collision_mats, tag=''):
        seq_to_plot_args = []
        for frame_i, (output, seq_to_sample_metrics) in enumerate(zip(outputs, all_sample_vals)):
            frame = output['frame']
            seq = output['seq']
            obs_traj = output['obs_motion'].numpy()
            assert obs_traj.shape[0] == 8
            pred_gt_traj = output['gt_motion'].numpy().swapaxes(0, 1)
            pred_fake_traj = output['pred_motion'].numpy().transpose(1, 2, 0, 3)  # (samples, ts, n_peds, 2)

            num_samples, _, n_ped, _ = pred_fake_traj.shape

            anim_save_fn = f'viz/{seq}/frame_{frame:06d}/{self.model_name}_epoch-{self.current_epoch}_{tag}.mp4'
            mkdir_if_missing(anim_save_fn)
            title = f"Seq: {seq} frame: {frame} Epoch: {self.current_epoch}"
            plot_args_list = {'save_fn': None, 'title': title, 'plot_size': (5, 4), }
            list_of_arg_dicts = []

            # pred_fake_traj_min = pred_fake_traj[argmins[frame_i],:,np.arange(n_ped)].swapaxes(0, 1)  # (n_ped, )
            # min_ADE_stats = get_metrics_str(dict(zip(stats_func.keys(), all_meters_values[frame_i])))
            if self.dataset_name == 'trajnet_sdd':
                bkg_img_path = os.path.join(
                    f'datasets/trajnet_sdd/reference_img/{seq[:-2]}/video{seq[-1]}/reference.jpg')
            else:
                bkg_img_path = None
            SADE_min_i = np.argmin(seq_to_sample_metrics['ADE'])
            pred_fake_traj_min = pred_fake_traj[SADE_min_i]
            min_SADE_stats = get_metrics_str(seq_to_sample_metrics, SADE_min_i)
            args_dict = {'plot_title': f"best mSADE sample",
                         'obs_traj': obs_traj,
                         'gt_traj': pred_gt_traj,
                         'pred_traj': pred_fake_traj_min,
                         'collision_mats': collision_mats[frame_i][-1],
                         'bkg_img_path': bkg_img_path,
                         'text_fixed': min_SADE_stats}
            list_of_arg_dicts.append(args_dict)

            for sample_i in range(num_samples - 1):
                stats = get_metrics_str(seq_to_sample_metrics, sample_i)
                args_dict = {'plot_title': f"Sample {sample_i}",
                             'obs_traj': obs_traj,
                             'gt_traj': pred_gt_traj,
                             'pred_traj': pred_fake_traj[sample_i],
                             'text_fixed': stats,
                             'bkg_img_path': bkg_img_path,
                             # 'highlight_peds': argmins[frame_i],
                             'collision_mats': collision_mats[frame_i][sample_i]}
                list_of_arg_dicts.append(args_dict)
                plot_args_list['list_of_arg_dicts'] = list_of_arg_dicts
            seq_to_plot_args.append(plot_args_list)

        if self.args.mp:
            with multiprocessing.Pool(self.num_workers) as pool:
                all_figs = pool.starmap(plot_anim_grid, seq_to_plot_args)

        else:
            all_figs = list(starmap(plot_anim_grid, seq_to_plot_args))

        return all_figs


    def _save_viz(self, outputs, all_sample_vals, all_meters_values, argmins, collision_mats, tag=''):
        seq_to_plot_args = []
        for frame_i, (output, seq_to_sample_metrics) in enumerate(zip(outputs, all_sample_vals)):
            frame = output['frame']
            seq = output['seq']
            obs_traj = output['obs_motion'].numpy()
            assert obs_traj.shape[0] == 8
            pred_gt_traj = output['gt_motion'].numpy().swapaxes(0, 1)
            pred_fake_traj = output['pred_motion'].numpy().transpose(1, 2, 0, 3)  # (samples, ts, n_peds, 2)

            num_samples, _, n_ped, _ = pred_fake_traj.shape

            anim_save_fn = f'viz/{seq}/frame_{frame:06d}/{self.model_name}_epoch-{self.current_epoch}_{tag}.mp4'
            mkdir_if_missing(anim_save_fn)
            title = f"Seq: {seq} frame: {frame} Epoch: {self.current_epoch}"
            plot_args_list = {'save_fn': None, 'title': title, 'plot_size': (5, 4),}
            list_of_arg_dicts = []

            # pred_fake_traj_min = pred_fake_traj[argmins[frame_i],:,np.arange(n_ped)].swapaxes(0, 1)  # (n_ped, )
            # min_ADE_stats = get_metrics_str(dict(zip(stats_func.keys(), all_meters_values[frame_i])))
            if self.dataset_name == 'trajnet_sdd':
                bkg_img_path = os.path.join(f'datasets/trajnet_sdd/reference_img/{seq[:-2]}/video{seq[-1]}/reference.jpg')
            else:
                bkg_img_path = None
            SADE_min_i = np.argmin(seq_to_sample_metrics['ADE'])
            pred_fake_traj_min = pred_fake_traj[SADE_min_i]
            min_SADE_stats = get_metrics_str(seq_to_sample_metrics, SADE_min_i)
            args_dict = {'plot_title': f"best mSADE sample",
                         'obs_traj': obs_traj,
                         'gt_traj': pred_gt_traj,
                         'pred_traj': pred_fake_traj_min,
                         'collision_mats': collision_mats[frame_i][-1],
                         'bkg_img_path': bkg_img_path,
                         'text_fixed': min_SADE_stats}
            list_of_arg_dicts.append(args_dict)

            for sample_i in range(num_samples - 1):
                stats = get_metrics_str(seq_to_sample_metrics, sample_i)
                args_dict = {'plot_title': f"Sample {sample_i}",
                             'obs_traj': obs_traj,
                             'gt_traj': pred_gt_traj,
                             'pred_traj': pred_fake_traj[sample_i],
                             'text_fixed': stats,
                             'bkg_img_path': bkg_img_path,
                             # 'highlight_peds': argmins[frame_i],
                             'collision_mats': collision_mats[frame_i][sample_i]}
                list_of_arg_dicts.append(args_dict)
                plot_args_list['list_of_arg_dicts'] = list_of_arg_dicts
            seq_to_plot_args.append(plot_args_list)

        if self.args.mp:
            with multiprocessing.Pool(self.num_workers) as pool:
                all_figs = pool.starmap(plot_anim_grid, seq_to_plot_args)

        else:
            all_figs = list(starmap(plot_anim_grid, seq_to_plot_args))

        return all_figs


    def train_epoch_end(self, outputs):
        self._epoch_end(outputs, 'train')
        self.model.step_annealer()

    def validation_epoch_end(self, outputs):
        self._epoch_end(outputs, 'val')

    def test_epoch_end(self, outputs):
        self._epoch_end(outputs)

    def on_load_checkpoint(self, checkpoint):
        if 'model_dict' in checkpoint and 'epoch' in checkpoint:
            checkpoint['state_dict'] = {f'model.{k}': v for k, v in checkpoint['model_dict'].items()}
            checkpoint['global_step'] = None  # checkpoint['epoch'] * jb
            checkpoint['lr_schedulers'] = [checkpoint['scheduler_dict']]
            checkpoint['optimizer_states'] = [checkpoint['opt_dict']]
        print(f"EPOCH {checkpoint['epoch']}")

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.model.parameters(), lr=self.cfg.lr)#, weight_decay=self.hparams.weight_decay)
        # scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, milestones=self.hparams.scheduler_step_size,
        #                                                  gamma=0.5)
        scheduler_type = self.cfg.get('lr_scheduler', 'linear')
        if scheduler_type == 'linear':
            scheduler = get_scheduler(optimizer, policy='lambda', nepoch_fix=self.cfg.lr_fix_epochs, nepoch=self.cfg.num_epochs)
        elif scheduler_type == 'step':
            scheduler = get_scheduler(optimizer, policy='step', decay_step=self.cfg.decay_step, decay_gamma=self.cfg.decay_gamma)
        else:
            raise ValueError('unknown scheduler type!')

        return [optimizer], [scheduler]
