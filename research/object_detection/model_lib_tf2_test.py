# Copyright 2017 The TensorFlow Authors. All Rights Reserved.
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
# ==============================================================================
"""Tests for object detection model library."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import tempfile
import unittest
import numpy as np
import six
import tensorflow.compat.v1 as tf

from object_detection import inputs
from object_detection import model_hparams
from object_detection import model_lib_v2
from object_detection.builders import model_builder
from object_detection.core import model
from object_detection.protos import train_pb2
from object_detection.utils import config_util
from object_detection.utils import tf_version

if six.PY2:
  import mock  # pylint: disable=g-importing-member,g-import-not-at-top
else:
  from unittest import mock  # pylint: disable=g-importing-member,g-import-not-at-top

# Model for test. Current options are:
# 'ssd_mobilenet_v2_pets_keras'
MODEL_NAME_FOR_TEST = 'ssd_mobilenet_v2_pets_keras'


def _get_data_path():
  """Returns an absolute path to TFRecord file."""
  return os.path.join(tf.resource_loader.get_data_files_path(), 'test_data',
                      'pets_examples.record')


def get_pipeline_config_path(model_name):
  """Returns path to the local pipeline config file."""
  return os.path.join(tf.resource_loader.get_data_files_path(), 'samples',
                      'configs', model_name + '.config')


def _get_labelmap_path():
  """Returns an absolute path to label map file."""
  return os.path.join(tf.resource_loader.get_data_files_path(), 'data',
                      'pet_label_map.pbtxt')


def _get_config_kwarg_overrides():
  """Returns overrides to the configs that insert the correct local paths."""
  data_path = _get_data_path()
  label_map_path = _get_labelmap_path()
  return {
      'train_input_path': data_path,
      'eval_input_path': data_path,
      'label_map_path': label_map_path
  }


@unittest.skipIf(tf_version.is_tf1(), 'Skipping TF2.X only test.')
class ModelLibTest(tf.test.TestCase):

  @classmethod
  def setUpClass(cls):  # pylint:disable=g-missing-super-call
    tf.keras.backend.clear_session()

  def test_train_loop_then_eval_loop(self):
    """Tests that Estimator and input function are constructed correctly."""
    hparams = model_hparams.create_hparams(
        hparams_overrides='load_pretrained=false')
    pipeline_config_path = get_pipeline_config_path(MODEL_NAME_FOR_TEST)
    config_kwarg_overrides = _get_config_kwarg_overrides()
    model_dir = tf.test.get_temp_dir()

    train_steps = 2
    model_lib_v2.train_loop(
        hparams,
        pipeline_config_path,
        model_dir=model_dir,
        train_steps=train_steps,
        checkpoint_every_n=1,
        **config_kwarg_overrides)

    model_lib_v2.eval_continuously(
        hparams,
        pipeline_config_path,
        model_dir=model_dir,
        checkpoint_dir=model_dir,
        train_steps=train_steps,
        wait_interval=1,
        timeout=10,
        **config_kwarg_overrides)


class SimpleModel(model.DetectionModel):
  """A model with a single weight vector."""

  def __init__(self, num_classes=1):
    super(SimpleModel, self).__init__(num_classes)
    self.weight = tf.keras.backend.variable(np.ones(10), name='weight')

  def postprocess(self, prediction_dict, true_image_shapes):
    return {}

  def updates(self):
    return []

  def restore_map(self, *args, **kwargs):
    return {'model': self}

  def preprocess(self, _):
    return tf.zeros((1, 128, 128, 3)), tf.constant([[128, 128, 3]])

  def provide_groundtruth(self, *args, **kwargs):
    pass

  def predict(self, pred_inputs, true_image_shapes):
    return {'prediction':
            tf.abs(tf.reduce_sum(self.weight) * tf.reduce_sum(pred_inputs))}

  def loss(self, prediction_dict, _):
    return {'loss': tf.reduce_sum(prediction_dict['prediction'])}

  def regularization_losses(self):
    return []


@unittest.skipIf(tf_version.is_tf1(), 'Skipping TF2.X only test.')
class ModelCheckpointTest(tf.test.TestCase):
  """Test for model checkpoint related functionality."""

  def test_checkpoint_max_to_keep(self):
    """Test that only the most recent checkpoints are kept."""

    with mock.patch.object(
        model_builder, 'build', autospec=True) as mock_builder:
      mock_builder.return_value = SimpleModel()

      hparams = model_hparams.create_hparams(
          hparams_overrides='load_pretrained=false')
      pipeline_config_path = get_pipeline_config_path(MODEL_NAME_FOR_TEST)
      config_kwarg_overrides = _get_config_kwarg_overrides()
      model_dir = tempfile.mkdtemp(dir=self.get_temp_dir())

      model_lib_v2.train_loop(
          hparams, pipeline_config_path, model_dir=model_dir,
          train_steps=20, checkpoint_every_n=2, checkpoint_max_to_keep=3,
          **config_kwarg_overrides
      )
      ckpt_files = tf.io.gfile.glob(os.path.join(model_dir, 'ckpt-*.index'))
      self.assertEqual(len(ckpt_files), 3,
                       '{} not of length 3.'.format(ckpt_files))


class IncompatibleModel(SimpleModel):

  def restore_map(self, *args, **kwargs):
    return {'weight': self.weight}


@unittest.skipIf(tf_version.is_tf1(), 'Skipping TF2.X only test.')
class CheckpointV2Test(tf.test.TestCase):

  def setUp(self):
    super(CheckpointV2Test, self).setUp()

    self._model = SimpleModel()
    tf.keras.backend.set_value(self._model.weight, np.ones(10) * 42)
    ckpt = tf.train.Checkpoint(model=self._model)

    self._test_dir = tf.test.get_temp_dir()
    self._ckpt_path = ckpt.save(os.path.join(self._test_dir, 'ckpt'))
    tf.keras.backend.set_value(self._model.weight, np.ones(10))

    pipeline_config_path = get_pipeline_config_path(MODEL_NAME_FOR_TEST)
    configs = config_util.get_configs_from_pipeline_file(pipeline_config_path)
    configs = config_util.merge_external_params_with_configs(
        configs, kwargs_dict=_get_config_kwarg_overrides())
    self._train_input_fn = inputs.create_train_input_fn(
        configs['train_config'],
        configs['train_input_config'],
        configs['model'])

  def test_restore_v2(self):
    """Test that restoring a v2 style checkpoint works."""

    model_lib_v2.load_fine_tune_checkpoint(
        self._model, self._ckpt_path, checkpoint_type='',
        checkpoint_version=train_pb2.CheckpointVersion.V2,
        load_all_detection_checkpoint_vars=True,
        input_dataset=self._train_input_fn(),
        unpad_groundtruth_tensors=True)
    np.testing.assert_allclose(self._model.weight.numpy(), 42)

  def test_restore_map_incompatible_error(self):
    """Test that restoring an incompatible restore map causes an error."""

    with self.assertRaisesRegex(TypeError,
                                r'.*received a \(str -> ResourceVariable\).*'):
      model_lib_v2.load_fine_tune_checkpoint(
          IncompatibleModel(), self._ckpt_path, checkpoint_type='',
          checkpoint_version=train_pb2.CheckpointVersion.V2,
          load_all_detection_checkpoint_vars=True,
          input_dataset=self._train_input_fn(),
          unpad_groundtruth_tensors=True)


