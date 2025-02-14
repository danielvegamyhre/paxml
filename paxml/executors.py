# coding=utf-8
# Copyright 2022 The Pax Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Implementations of program executors."""

import contextlib
import functools
import gc
from typing import Any, Callable, Optional, Sequence, Tuple

from absl import logging
from etils import epath
import jax
from paxml import base_executor
from paxml import eval_lib
from paxml import partitioning
from paxml import programs
from paxml import summary_utils
from paxml import tasks_lib
from paxml import train_states
from paxml import trainer_lib
from paxml import tuning_lib
from praxis import base_hyperparams
from praxis import base_input
from praxis import base_layer
from praxis import pax_fiddle
from praxis import py_utils
from praxis import pytypes
import tensorflow.compat.v2 as tf

from paxml import checkpoints  # mapped to internal

instantiate = base_hyperparams.instantiate
RunningMode = trainer_lib.RunningMode
SummaryWriter = tf.summary.SummaryWriter
TrainState = train_states.TrainState
TrainStateProvenance = train_states.TrainStateProvenance


def _maybe_update_latest_model_step(
    train_input_p: pax_fiddle.Config[base_input.BaseInput],
    initial_global_step: Optional[int],
    task_p: pax_fiddle.Config[tasks_lib.SingleTask],
) -> None:
  """Updates `train_input_p` in place its latest model step."""
  if not hasattr(train_input_p, 'deterministic_input_start_index'):
    # Not deterministic seqio.
    return
  logging.info(f'step used for deterministic seqio: {initial_global_step}')
  if initial_global_step is None:
    if task_p.train.external_checkpoint_path:
      logging.warning(
          'Disabling deterministic SeqIO since it will restore from external'
          ' checkpoint, and the step number is not known beforehand.'
      )
    # When not restoring from external_checkpoint_path, it means no checkpoint
    # to restore in this case and it'll train from step 0, so no need to update.
    return
  logging.info('Updating _latest_model_step for training input.')
  dp = train_input_p.deterministic_input_start_index
  dp._latest_model_step = (
      initial_global_step  # pylint: disable=protected-access
  )


class _DecodeSummaryWriters(contextlib.ExitStack):
  """Manage decode summary writers."""

  _exit_callbacks = []

  def __init__(
      self, job_log_dir: epath.Path, decode_input_names: Sequence[str]
  ):
    """Initialize context manager.

    Args:
      job_log_dir: Directory for the job logs.
      decode_input_names: list of names for the decode input pipelines.
    """
    super().__init__()
    self.summary_decode_dirs = [
        job_log_dir / 'summaries' / f'decode_test_{name}'
        for name in decode_input_names
    ]

  def __enter__(self) -> Sequence[SummaryWriter]:
    self.decode_summary_writers = [
        self.enter_context(summary_utils.get_summary_writer(d))
        for d in self.summary_decode_dirs
    ]
    return self.decode_summary_writers


class DefaultExecutor(base_executor.BaseExecutor):
  """The default executor for running programs."""

  def __init__(self):
    super().__init__()

    # States to set in .setup().
    self._job_log_dir: epath.Path = None
    self._early_stopping_fn = None
    self._task: tasks_lib.SingleTask = None
    self._checkpointer: checkpoints.TrainingCheckpointer = None
    self._partitioner: partitioning.Partitioner = None
    self._decode_input_ps = None
    self._train_program: programs.BaseTrainProgram = None
    self._eval_programs: Sequence[programs.BaseEvalProgram] = None

    # States to lazily initialize in .setup().
    self._train_input_pipeline = None
    self._partitioned_train_state = None
    self._train_state_provenance = None
    self._total_num_params = None
    self._prng_key = None
    self._train_prng_seed = None
    self._eval_prng_seed = None

  def _maybe_create_train_input(
      self,
      task_p: pax_fiddle.Config[tasks_lib.SingleTask],
      step: Optional[int],
      train_input_p: pax_fiddle.Config[base_input.BaseInput],
  ) -> Tuple[
      Optional[base_input.BaseInput],
      Optional[base_input.BaseInput],
      Optional[base_input.BaseInput],
  ]:
    """Optionally creates the train input for partitioner and checkpointing.

    Args:
      task_p: The task config.
      step: The step number of the checkpoint to restore from. If None, means no
        checkpoint to restore.
      train_input_p: The config for the train input pipeline.

    Returns:
      A 3-tuple (train_input, train_input_for_partitioner,
      train_input_for_checkpoint), where:

      - train_input: the train input pipeline.
      - train_input_for_partitioner: represents the train_input_pipeline arg
        passed to partitioner.setup(). If set, the partitioner will use it to
        get the shape/dtype information for model.init.
      - train_input_for_checkpoint: represents the train_input_pipeline arg
        passed to checkpointer.get_model_states(). If set, the checkpointer will
        restore its states from checkpoint.
    """
    if not task_p.train.enable_input_checkpointing:
      _maybe_update_latest_model_step(train_input_p, step, task_p)
    train_input = instantiate(train_input_p)

    train_input_for_partitioner = (
        None if task_p.train.enforce_input_specs else train_input
    )
    train_input_for_checkpoint = (
        train_input if task_p.train.enable_input_checkpointing else None
    )
    return train_input, train_input_for_partitioner, train_input_for_checkpoint

  def setup(
      self,
      jax_task: tasks_lib.SingleTask,
      job_log_dir: epath.Path,
      checkpointer: Any,
      partitioner: partitioning.Partitioner,
      input_specs_provider: base_input.BaseInputSpecsProvider,
      train_input_p: pax_fiddle.Config[base_input.BaseInput],
      decode_input_ps: Sequence[pax_fiddle.Config[base_input.BaseInput]],
      train_program: programs.BaseTrainProgram,
      eval_programs: Sequence[programs.BaseEvalProgram],
      early_stopping_fn: Optional[trainer_lib.EarlyStoppingFn],
      exit_after_ondemand_checkpoint: bool = False,
  ):
    self._task = jax_task
    self._job_log_dir = job_log_dir
    self._checkpointer = checkpointer
    self._partitioner = partitioner
    self._decode_input_ps = decode_input_ps
    self._train_program = train_program
    self._eval_programs = eval_programs
    self._early_stopping_fn = early_stopping_fn
    self._exit_after_ondemand_checkpoint = exit_after_ondemand_checkpoint
    task_p = jax_task.hparams

    # Creates the root prng key and train input pipeline.
    root_prng_key = jax.random.PRNGKey(task_p.train.random_seed)
    train_input_p = partitioner.preprocess_input_config(train_input_p)
    train_input, train_input_for_partitioner, train_input_for_checkpoint = (
        self._maybe_create_train_input(
            task_p, checkpointer.step_to_restore, train_input_p
        )
    )

    # Sets up the partitioner. Note it only needs shape/dtype information of the
    # prng key.
    # TODO(laigd): let it take ShapeDtypeStruct of prng key instead.
    train_input_specs = None
    if task_p.train.enforce_input_specs:
      # TODO(laigd): the batch size used in the spec is inconsistent with
      # BaseInput.get_next_padded(), fix it.
      train_input_specs = trainer_lib.get_train_input_specs(
          task_p, input_specs_provider
      )
      if not train_input_specs:
        raise ValueError(
            'No training input specs available, while enabling '
            '`task_p.train.enforce_input_specs` requires it.'
        )
    partitioner.setup(
        jax_task,
        root_prng_key,
        train_inputs_shape_dtype=train_input_specs,
        train_input_pipeline=train_input_for_partitioner,
        job_log_dir=job_log_dir,
    )
    train_state_metadata = partitioner.get_train_state_metadata()

    # JaxContext needed for shared layer lookup from global scope.
    with base_layer.JaxContext.new_context():
      # Dump out model meta info for debugging.
      trainer_lib.write_post_init_model_hparams_file(
          jax_task.model, train_state_metadata.var_weight_hparams, job_log_dir
      )

    # Restore TrainState from checkpoint or initialize it.
    (
        partitioned_train_state,
        train_state_provenance,
        total_num_params,
        root_prng_key,
    ) = checkpointer.get_model_states(
        partitioner,
        train_state_metadata,
        root_prng_key,
        train_input_for_checkpoint,
    )
    if train_state_provenance:
      trainer_lib.write_train_provenance_file(
          train_state_provenance, job_log_dir
      )

    # Splits the key.
    prng_key, train_prng_seed, eval_prng_seed = jax.random.split(
        root_prng_key, 3
    )
    logging.info('train prng seed: %s', train_prng_seed)
    logging.info('eval prng seed: %s', eval_prng_seed)
    train_prng_seed = partitioner.preprocess_prng_key(train_prng_seed)
    eval_prng_seed = partitioner.preprocess_prng_key(eval_prng_seed)

    # Sets the lazily initialized states.
    self._train_input_pipeline = train_input
    self._partitioned_train_state = partitioned_train_state
    self._train_state_provenance = train_state_provenance
    self._total_num_params = total_num_params
    self._prng_key = prng_key
    self._train_prng_seed = train_prng_seed
    self._eval_prng_seed = eval_prng_seed

  def _create_decode_programs(self, decode_input_params):
    # TODO(wangpeng): Make decode programs configurable.
    create_decode_program = functools.partial(
        eval_lib.SingleTaskDecodeProgram,
        model=self._task.model,
        partitioner=self._partitioner,
    )
    decode_programs = [
        create_decode_program(decode_input=instantiate(p), input_index=i)
        for i, p in enumerate(decode_input_params)
    ]
    trainer_lib.check_unique_names([p.decode_input for p in decode_programs])
    return decode_programs

  def partition_decode_once_fns(
      self,
      prng_key: jax.random.KeyArray,
      decode_input_ps: Sequence[pax_fiddle.Config[base_input.BaseInput]],
  ) -> Tuple[
      Callable[..., tuning_lib.DecodeMetrics],
      jax.random.KeyArray,
      Sequence[str],
  ]:
    use_pmap = self._task.model.ici_mesh_shape is None

    assert decode_input_ps, 'decode_input_p must not be empty'

    prng_key, decode_key = jax.random.split(prng_key, 2)
    logging.info(
        'decode %s: %s', 'prng_seed' if use_pmap else 'prng_key', decode_key
    )
    decode_key = self._partitioner.preprocess_prng_key(decode_key)

    preprocessed_decode_input_ps = [
        self._partitioner.preprocess_input_config(input_p)
        for input_p in decode_input_ps
    ]

    decode_programs = self._create_decode_programs(preprocessed_decode_input_ps)

    if use_pmap:
      var_weight_params = (
          self._partitioner.get_train_state_metadata().var_weight_hparams
      )
      spmd_decode_step = None
      decode_input_partition_spec = None
    else:
      var_weight_params = None

      _, decode_inputs_shape_dtype = trainer_lib.get_inputs_shape_dtype(
          preprocessed_decode_input_ps[0]
      )

      # TODO(pax-dev): Support auto-sharding for decoder step.
      step_fn, is_eval = partitioning.get_step_fn(RunningMode.DECODE)
      assert is_eval
      spmd_decode_step, decode_input_partition_spec = (
          self._partitioner.partition(
              step_fn, decode_inputs_shape_dtype, is_eval
          )
      )

    decode_once_fn = eval_lib.partitioned_decode_once(
        decode_programs=decode_programs,
        task_p=self._task.hparams,
        job_log_dir=self._job_log_dir,
        prng_key=decode_key,
        use_pmap=use_pmap,
        var_weight_params=var_weight_params,
        spmd_decode_step=spmd_decode_step,
        inputs_partition_spec=decode_input_partition_spec,
    )

    decode_input_names = [p.decode_input.name for p in decode_programs]
    return decode_once_fn, prng_key, decode_input_names

  def start(self):
    is_vars_replicated = self._task.model.ici_mesh_shape is None
    _train_and_evaluate_common(
        self._task,
        self._partitioner,
        self._train_program,
        self._train_input_pipeline,
        self._partitioned_train_state,
        self._train_state_provenance,
        self._prng_key,
        self._eval_programs,
        self._decode_input_ps,
        self._total_num_params,
        self._early_stopping_fn,
        self._checkpointer,
        self.partition_decode_once_fns,
        self._job_log_dir,
        self._eval_prng_seed,
        is_vars_replicated,
        self._train_prng_seed,
        self._exit_after_ondemand_checkpoint,
    )

    # Shutdown the programs and run necessary cleanup.
    self._train_program.shutdown()
    for program in self._eval_programs:
      program.shutdown()


def _train_and_evaluate_common(
    task: tasks_lib.SingleTask,
    partitioner: partitioning.Partitioner,
    train_program: programs.BaseTrainProgram,
    train_input: base_input.BaseInput,
    partitioned_train_state: TrainState,
    train_state_provenance: TrainStateProvenance,
    prng_key,
    # TODO(hthu): Take a more generalized form of EvalProgram interface.
    eval_programs: Sequence[programs.BaseEvalProgram],
    decode_input_p,
    total_num_params,
    early_stopping_fn,
    checkpointer,
    partition_decode_once_fns,
    job_log_dir,
    eval_prng_seed,
    is_vars_replicated,
    train_prng_seed,
    exit_after_ondemand_checkpoint,
):
  """Training loop code common to both pmap and spmd."""
  task_p = task.hparams
  train_p = task_p.train
  train_state_metadata = partitioner.get_train_state_metadata()
  train_input_for_checkpoint = (
      train_input if train_p.enable_input_checkpointing else None
  )

  if decode_input_p:
    decode_once_fn, prng_key, decode_input_names = partition_decode_once_fns(
        prng_key, decode_input_p
    )
  else:
    decode_input_names = []

  initial_global_step = int(
      py_utils.maybe_unreplicate_for_fully_replicated(
          partitioned_train_state.step
      )
  )
  logging.info('Model initial global_step=%d', initial_global_step)
  if checkpointer.step_to_restore is not None:
    assert checkpointer.step_to_restore == initial_global_step, (
        f'Checkpoint number {checkpointer.step_to_restore} and restored step'
        f' number {initial_global_step} mismatch.'
    )

  logging.info('Training loop starting...')
  with _DecodeSummaryWriters(
      job_log_dir, decode_input_names
  ) as decode_summary_writers:
    step_i = initial_global_step

    # Sets up the programs.
    train_program.setup(
        task,
        train_input,
        partitioner,
        job_log_dir,
        train_prng_seed,
        eval_prng_seed,
        step_i,
    )
    for program in eval_programs:
      program.setup(task, partitioner, job_log_dir, eval_prng_seed)
    trainer_lib.check_unique_names([prog.eval_input for prog in eval_programs])

    train_summary_writer = train_program.summary_writer
    # This only prints the view from the first host machine.
    summary_utils.write_model_structure(
        train_summary_writer, partitioned_train_state, is_vars_replicated
    )
    # train_state_provenance is None when model restored from checkpoint
    if train_state_provenance:
      summary_utils.write_model_provenance(
          train_summary_writer, train_state_provenance
      )
    summary_utils.write_total_num_params(train_summary_writer, total_num_params)
    summary_utils.write_global_batch_size(
        train_summary_writer, train_program.train_unpadded_global_batch_size
    )

    # Start the train loop. Make sure all at the same step.
    py_utils.sync_global_devices(f'Start training loop from step: {step_i}')
    # Collect then freeze GC, so that GC in the training loop will not touch the
    # python objects used to initialize the model. Unfreeze at the end of the
    # loop.
    gc.collect()
    gc.freeze()
    while True:
      logging.debug('step=`%d`: Beginning', step_i)
      checkpointer.save_if_needed(
          step_i,
          partitioned_train_state,
          train_state_metadata.unpadded_global_shapes,
          train_state_metadata.partition_specs,
          train_input_for_checkpoint,
      )
      if exit_after_ondemand_checkpoint and checkpointer.reached_preemption(
          step_i
      ):
        checkpointer.wait_until_finished()
        exit(1)

      if not train_program.should_run(partitioned_train_state, step_i):
        logging.info(
            (
                'Training loop completed (step (`%d`) greater than '
                'num_train_step (`%d`).'
            ),
            step_i,
            train_p.num_train_steps,
        )
        break

      program_output = train_program.run(partitioned_train_state, step_i)
      partitioned_train_state = program_output.state
      train_weighted_scalars = program_output.aux.weighted_scalars
      steps_per_sec = program_output.aux.steps_per_sec
      eval_train_metrics = program_output.aux.eval_train_metrics

      # While the eval ones below are post-model weight updates, hence the step
      # counter is incremented in between.
      step_i = program_output.aux.new_train_step

      eval_metrics: Optional[tuning_lib.EvalMetrics] = None
      # Run eval at regular step interval.
      if (
          train_p.eval_interval_steps
          and step_i % train_p.eval_interval_steps == 0
      ):
        logging.debug('  Starting eval_step().')
        eval_partitioned_train_state = programs.get_eval_train_state(
            task, partitioned_train_state
        )
        # If we have eval test then also evaluate on test.
        if eval_programs:
          logging.debug('  Performing eval_step() runs on test splits.')
          with py_utils.timeit() as eval_period:
            eval_metrics_list, eval_scoring_metrics_list, num_eval_steps = (
                eval_lib.run_eval_loop_over_test_splits(
                    eval_programs,
                    eval_partitioned_train_state,
                    eval_prng_seed,
                    step_i,
                    job_log_dir,
                )
            )
          eval_steps_per_sec = sum(num_eval_steps) / eval_period.elapsed
          eval_metrics = tuning_lib.EvalMetrics(
              metrics_list=eval_metrics_list,
              scoring_metrics_list=eval_scoring_metrics_list,
              steps_per_sec=eval_steps_per_sec,
              input_names=[prog.eval_input.name for prog in eval_programs],
          )
          logging.debug(
              '  Completed eval_step() runs on test splits in %f seconds.',
              eval_period.elapsed,
          )

      decode_metrics: Optional[tuning_lib.DecodeMetrics] = None
      if (
          decode_input_p
          and train_p.decode_interval_steps
          and step_i % train_p.decode_interval_steps == 0
      ):
        if train_p.decode_use_ema_states:
          if not tasks_lib.has_ema(task_p):
            raise ValueError(
                'decode_use_ema_states is requested but the '
                'learner does not seem to have ema enabled'
            )
          decode_partitioned_train_state = tasks_lib.extract_ema(
              partitioned_train_state
          )
          logging.debug('  Performing decode_once_fn() with ema states.')
        else:
          decode_partitioned_train_state = partitioned_train_state
        decode_metrics = decode_once_fn(
            decode_partitioned_train_state, decode_summary_writers
        )

      logging.debug('step=`%d`: End', step_i - 1)

      if early_stopping_fn is not None:
        if tuning_lib.should_early_stop(
            early_stopping_fn,
            step_i,
            is_last_ckpt=tuning_lib.is_last_checkpoint(
                RunningMode.detect(
                    has_train_metrics=True,
                    has_eval_metrics=bool(eval_metrics),
                    has_decode_metrics=bool(decode_metrics),
                ),
                step_i,
                task_p.train.num_train_steps,
                task_p.train.eval_interval_steps,
                task_p.train.decode_interval_steps,
                task_p.train.save_interval_steps,
                train_to_end=getattr(
                    early_stopping_fn, 'train_to_end', False)
            ),
            train_weighted_scalars=train_weighted_scalars,
            eval_train_metrics=eval_train_metrics,
            eval_metrics=eval_metrics,
            decode_metrics=decode_metrics,
            train_steps_per_sec=steps_per_sec,
            num_params=total_num_params,
        ):
          logging.info(
              (
                  'Training loop is early stopped at step `%d` by the '
                  'tuner, while num_train_step is `%d`.'
              ),
              step_i,
              train_p.num_train_steps,
          )
          break
    gc.unfreeze()
    # Save checkpoint for the last step.
    checkpointer.save_final(
        step_i,
        partitioned_train_state=partitioned_train_state,
        train_state_unpadded_shape_dtype_struct=train_state_metadata.unpadded_global_shapes,
        train_state_pspecs=train_state_metadata.partition_specs,
        train_input_pipeline=train_input_for_checkpoint,
    )

    checkpointer.wait_until_finished()
