# Lint as: python2, python3
# Copyright 2019 Google LLC. All Rights Reserved.
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
"""Chicago taxi example using TFX."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import abc
import json
import multiprocessing
import os
from typing import Any, Dict, List, Optional, Text

import absl
import tensorflow_model_analysis as tfma

from tfx.components import CsvExampleGen
from tfx.components import Evaluator
from tfx.components import ExampleValidator
from tfx.components import Pusher
from tfx.components import ResolverNode
from tfx.components import SchemaGen
from tfx.components import StatisticsGen
from tfx.components import Trainer
from tfx.components import Transform
from tfx.dsl.experimental import latest_blessed_model_resolver
from tfx.orchestration import metadata
from tfx.orchestration import pipeline
from tfx.orchestration.beam.beam_dag_runner import BeamDagRunner
from tfx.proto import pusher_pb2
from tfx.proto import trainer_pb2
from tfx.types import Channel
from tfx.types.standard_artifacts import Model
from tfx.types.standard_artifacts import ModelBlessing
from tfx.types import standard_artifacts 
from tfx.utils.dsl_utils import external_input
from tfx.components.base.base_executor import BaseExecutor

from tfx import types
from tfx.types import artifact_utils
from tfx.utils import telemetry_utils
from tfx.utils import dependency_utils
from tfx.types.artifact import Artifact
from functools import reduce

from tfx.experimental.mock_units.mock_factory import FakeComponentExecutorFactory
from unittest.mock import patch
# from tfx.components.example_gen.base_example_gen_executor import 

_pipeline_name = 'chicago_taxi_beam'

# This example assumes that the taxi data is stored in ~/taxi/data and the
# taxi utility function is in ~/taxi.  Feel free to customize this as needed.
# _taxi_root = os.path.join(os.environ['HOME'], 'taxi')
_taxi_root = '/usr/local/google/home/sujip/tfx/tfx/examples/chicago_taxi_pipeline'
_data_root = os.path.join(_taxi_root, 'data', 'simple')
# Python module file to inject customized logic into the TFX components. The
# Transform and Trainer both require user-defined functions to run successfully.
_module_file = os.path.join(_taxi_root, 'taxi_utils.py')
# Path which can be listened to by the model server.  Pusher will output the
# trained model here.
_serving_model_dir = os.path.join(_taxi_root, 'serving_model', _pipeline_name)

# Directory and data locations.  This example assumes all of the chicago taxi
# example code and metadata library is relative to $HOME, but you can store
# these files anywhere on your local filesystem.
_tfx_root = os.path.join(os.environ['HOME'], 'tfx')
_pipeline_root = os.path.join(_tfx_root, 'pipelines', _pipeline_name)
# Sqlite ML-metadata db path.
_metadata_path = os.path.join(_tfx_root, 'metadata', _pipeline_name,
                              'metadata.db')


class DummyExecutor(BaseExecutor):
  def compare_artifacts(self, artifacts_lt: List[Artifact], artifacts_rt: List[Artifact]) -> bool:
    # assert 
    if len(artifacts_lt) == len(artifacts_rt):
      return False
    for artifact_lt in artifacts_lt:
      absl.logging.info("artifact_lt.name %s", artifact_lt.artifact_type.name)
      for artifact_rt in artifacts_rt:
        if artifact_lt.artifact_type.name == artifact_rt.artifact_type.name:
          if artifact_lt.uri != artifact_rt.uri:
            return False
      else:
        return False
    return True

  def Do(self, input_dict: Dict[Text, List[types.Artifact]],
         output_dict: Dict[Text, List[types.Artifact]],
         exec_properties: Dict[Text, Any]) -> None:
    self.input_dict, self.output_dict, self.exec_properties = input_dict, output_dict, exec_properties

  def check_artifacts(self, input_artifacts: List[Artifact], output_artifacts:List[Artifact]):
    inputs, outputs = reduce(lambda x,y:x+y,list(self.input_dict.values())), reduce(lambda x,y:x+y, list(self.output_dict.values()))

    if not self.compare_artifacts(inputs, input_artifacts):
      raise Exception('Test failed\nPusher got = "{}", want = "{}"'.format(inputs, input_artifacts))
    if not self.compare_artifacts(outputs, output_artifacts):
      raise Exception('Test failed\nPusher got = "{}", want = "{}"'.format(outputs, output_artifacts))

# TODO(b/137289334): rename this as simple after DAG visualization is done.
def _create_pipeline(pipeline_name: Text, pipeline_root: Text, data_root: Text,
                     module_file: Text, serving_model_dir: Text,
                     metadata_path: Text,
                     direct_num_workers: int) -> pipeline.Pipeline:
  """Implements the chicago taxi pipeline with TFX."""
  examples = external_input(data_root)

  # Brings data into the pipeline or otherwise joins/converts training data.
  example_gen = CsvExampleGen(input=examples)

  # Computes statistics over data for visualization and example validation.
  statistics_gen = StatisticsGen(examples=example_gen.outputs['examples'])

  # Generates schema based on statistics files.
  schema_gen = SchemaGen(
      statistics=statistics_gen.outputs['statistics'],
      infer_feature_shape=False)

  # Performs anomaly detection based on statistics and data schema.
  example_validator = ExampleValidator(
      statistics=statistics_gen.outputs['statistics'],
      schema=schema_gen.outputs['schema'])

  # Performs transformations and feature engineering in training and serving.
  transform = Transform(
      examples=example_gen.outputs['examples'],
      schema=schema_gen.outputs['schema'],
      module_file=module_file)

  # Uses user-provided Python function that implements a model using TF-Learn.
  trainer = Trainer(
      module_file=module_file,
      transformed_examples=transform.outputs['transformed_examples'],
      schema=schema_gen.outputs['schema'],
      transform_graph=transform.outputs['transform_graph'],
      train_args=trainer_pb2.TrainArgs(num_steps=10000),
      eval_args=trainer_pb2.EvalArgs(num_steps=5000))

  # Get the latest blessed model for model validation.
  model_resolver = ResolverNode(
      instance_name='latest_blessed_model_resolver',
      resolver_class=latest_blessed_model_resolver.LatestBlessedModelResolver,
      model=Channel(type=Model),
      model_blessing=Channel(type=ModelBlessing))

  # Uses TFMA to compute a evaluation statistics over features of a model and
  # perform quality validation of a candidate model (compared to a baseline).
  eval_config = tfma.EvalConfig(
      model_specs=[tfma.ModelSpec(signature_name='eval')],
      slicing_specs=[
          tfma.SlicingSpec(),
          tfma.SlicingSpec(feature_keys=['trip_start_hour'])
      ],
      metrics_specs=[
          tfma.MetricsSpec(
              thresholds={
                  'accuracy':
                      tfma.config.MetricThreshold(
                          value_threshold=tfma.GenericValueThreshold(
                              lower_bound={'value': 0.6}),
                          change_threshold=tfma.GenericChangeThreshold(
                              direction=tfma.MetricDirection.HIGHER_IS_BETTER,
                              absolute={'value': -1e-10}))
              })
      ])
  evaluator = Evaluator(
      examples=example_gen.outputs['examples'],
      model=trainer.outputs['model'],
      baseline_model=model_resolver.outputs['model'],
      # Change threshold will be ignored if there is no baseline (first run).
      eval_config=eval_config)

  # Checks whether the model passed the validation steps and pushes the model
  # to a file destination if check passed.
  pusher = Pusher(
      model=trainer.outputs['model'],
      model_blessing=evaluator.outputs['blessing'],
      push_destination=pusher_pb2.PushDestination(
          filesystem=pusher_pb2.PushDestination.Filesystem(
              base_directory=serving_model_dir)))


  return pipeline.Pipeline(
      pipeline_name=pipeline_name,
      pipeline_root=pipeline_root,
      components=[
          example_gen,
          statistics_gen,
          schema_gen,
          example_validator,
          transform,
          trainer,
          model_resolver,
          evaluator,
          pusher,
      ],
      # enable_cache=True,
      metadata_connection_config=metadata.sqlite_metadata_connection_config(
          metadata_path),
      # TODO(b/142684737): The multi-processing API might change.
      beam_pipeline_args=['--direct_num_workers=%d' % direct_num_workers])

# @patch('tfx.components.example_gen.csv_example_gen.executor.Executor')
def test():#W(MockExecutor):
  # absl.logging.info("mockexecutor %s", MockExecutor)
  external_artifact = standard_artifacts.ExternalArtifact()
  external_artifact.uri = '/usr/local/google/home/sujip/tfx/tfx/examples/chicago_taxi_pipeline/data/simple'

  examples = standard_artifacts.Examples()
  examples.uri = '/usr/local/google/home/sujip/tfx/pipelines/chicago_taxi_beam/CsvExampleGen/examples/2'
  # examples.split_names = artifact_utils.encode_split_names(['train', 'eval'])

  example_statistics=standard_artifacts.ExampleStatistics()
  example_statistics.uri = '/usr/local/google/home/sujip/tfx/pipelines/chicago_taxi_beam/StatisticsGen/statistics/70'
  # statistics.split_names = artifact_utils.encode_split_names(['train', 'eval'])

  schema=standard_artifacts.Schema()
  schema.uri = '/usr/local/google/home/sujip/tfx/pipelines/chicago_taxi_beam/SchemaGen/schema/71'

  anomalies=standard_artifacts.ExampleAnomalies()
  anomalies.uri = '/usr/local/google/home/sujip/tfx/pipelines/chicago_taxi_beam/ExampleValidator/anomalies/76'

  # validation_output = 

  transform_graph=standard_artifacts.TransformGraph()
  transform_graph.uri='/usr/local/google/home/sujip/tfx/pipelines/chicago_taxi_beam/Transform/transform_graph/72'

  transformed_examples=standard_artifacts.Examples()
  transformed_examples.uri='/usr/local/google/home/sujip/tfx/pipelines/chicago_taxi_beam/Transform/transformed_examples/72'
  model=standard_artifacts.Model()
  model.uri='/usr/local/google/home/sujip/tfx/pipelines/chicago_taxi_beam/Trainer/model/73'

  baseline_model=standard_artifacts.Model()

  evaluation=standard_artifacts.ModelEvaluation()
  evaluation.uri='/usr/local/google/home/sujip/tfx/pipelines/chicago_taxi_beam/Evaluator/evaluation/74'

  model_blessing=standard_artifacts.ModelBlessing()
  model_blessing.uri='/usr/local/google/home/sujip/tfx/pipelines/chicago_taxi_beam/Evaluator/blessing/74'

  pushed_model=standard_artifacts.PushedModel()
  pushed_model.uri='/usr/local/google/home/sujip/tfx/pipelines/chicago_taxi_beam/Pusher/pushed_model/75'

  pipeline = _create_pipeline(
          pipeline_name=_pipeline_name,
          pipeline_root=_pipeline_root,
          data_root=_data_root,
          module_file=_module_file,
          serving_model_dir=_serving_model_dir,
          metadata_path=_metadata_path,
          # 0 means auto-detect based on the number of CPUs available during
          # execution time.
          direct_num_workers=0) # pipeline dsl
  #DummyExecutor MockExecutor
  pipeline.set_executor('CsvExampleGen', FakeComponentExecutorFactory, [external_artifact], [examples])
  pipeline.set_executor('StatisticsGen', FakeComponentExecutorFactory, [examples], [example_statistics])
  pipeline.set_executor('SchemaGen',  FakeComponentExecutorFactory, [example_statistics], [schema])
  pipeline.set_executor('ExampleValidator',  FakeComponentExecutorFactory, [example_statistics, schema], [anomalies])
  pipeline.set_executor('Transform',  FakeComponentExecutorFactory, [examples, schema], [transform_graph, transformed_examples])
  pipeline.set_executor('Trainer',  FakeComponentExecutorFactory, [examples, transform_graph, schema], [model])
  # pipeline.set_executor('ResolverNode.latest_blessed_model_resolver',  DummyExecutor, [])
  pipeline.set_executor('Evaluator',  FakeComponentExecutorFactory, [examples, model, baseline_model], [evaluation, model_blessing])
  pipeline.set_executor('Pusher',  FakeComponentExecutorFactory, [model, model_blessing], [pushed_model])

  BeamDagRunner().run(pipeline)


if __name__ == '__main__':
  absl.logging.set_verbosity(absl.logging.INFO)
  test()