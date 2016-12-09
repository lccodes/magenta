# Copyright 2016 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Event sequence RNN model."""

import copy
import heapq

# internal imports

import numpy as np
from six.moves import range  # pylint: disable=redefined-builtin
import tensorflow as tf

import magenta.models.shared.vl_rnn_graph as vl_rnn_graph
import magenta.models.shared.events_rnn_graph as events_rnn_graph
import magenta.music as mm


class EventSequenceRnnModelException(Exception):
  pass


class EventSequenceRnnModel(mm.BaseModel):
  """Class for RNN event sequence generation models.

  Currently this class only supports generation, of both event sequences and
  note sequences (via event sequences). Support for model training will be added
  at a later time.
  """

  def __init__(self, config):
    """Initialize the EventSequenceRnnModel.

    Args:
      config: An EventSequenceRnnConfig containing the encoder/decoder and
        HParams to use.
    """
    super(EventSequenceRnnModel, self).__init__()
    self._config = config

    # Override hparams for generation.
    # TODO(fjord): once this class supports training, make this step conditional
    # on the usage mode.
    self._config.hparams.dropout_keep_prob = 1.0
    self._config.hparams.batch_size = 1

  def _build_graph_for_generation(self):
    return vl_rnn_graph.build_graph('generate', self._config)

  def _generate_step_for_batch(self, event_sequences, inputs, initial_state,
                               temperature):
    """Extends a batch of event sequences by a single step each.

    This method modifies the event sequences in place.

    Args:
      event_sequences: A list of event sequences, each of which is a Python
          list-like object. The list of event sequences should have length equal
          to `self._config.hparams.batch_size`.
      inputs: A Python list of model inputs, with length equal to
          `self._config.hparams.batch_size`.
      initial_state: A numpy array containing the initial RNN state, where
          `initial_state.shape[0]` is equal to
          `self._config.hparams.batch_size`.
      temperature: The softmax temperature.

    Returns:
      final_state: The final RNN state, a numpy array the same size as
          `initial_state`.
      softmax: The chosen softmax value for each event sequence, a 1-D numpy
          array of length `self._config.hparams.batch_size`.
    """
    assert len(event_sequences) == self._config.hparams.batch_size

    graph_inputs = self._session.graph.get_collection('inputs')[0]
    graph_initial_state = self._session.graph.get_collection('initial_state')[0]
    graph_final_state = self._session.graph.get_collection('final_state')[0]
    graph_softmax = self._session.graph.get_collection('softmax')[0]
    graph_temperature = self._session.graph.get_collection('temperature')

    feed_dict = {graph_inputs: inputs, graph_initial_state: initial_state}
    # For backwards compatibility, we only try to pass temperature if the
    # placeholder exists in the graph.
    if graph_temperature:
      feed_dict[graph_temperature[0]] = temperature
    final_state, softmax = self._session.run(
        [graph_final_state, graph_softmax], feed_dict)
    indices = self._config.encoder_decoder.extend_event_sequences(
        event_sequences, softmax)

    return final_state, softmax[range(len(event_sequences)), -1, indices]

  def _generate_step(self, event_sequences, inputs, initial_state, temperature):
    """Extends a list of event sequences by a single step each.

    This method modifies the event sequences in place.

    Args:
      event_sequences: A list of event sequence objects.
      inputs: A Python list of model inputs, with length equal to the number of
          event sequences.
      initial_state: A numpy array containing the initial RNN states, where
          `initial_state.shape[0]` is equal to the number of event sequences.
      temperature: The softmax temperature.

    Returns:
      final_state: The final RNN state, a numpy array the same size as
          `initial_state`.
      softmax: The chosen softmax value for each event sequence, a 1-D numpy
          array the same length as `event_sequences`.
    """
    batch_size = self._config.hparams.batch_size
    num_full_batches = len(event_sequences) / batch_size

    final_state = np.empty((len(event_sequences), initial_state.shape[1]))
    softmax = np.empty(len(event_sequences))

    offset = 0
    for _ in range(num_full_batches):
      # Generate a single step for one batch of event sequences.
      batch_indices = range(offset, offset + batch_size)
      batch_final_state, batch_softmax = self._generate_step_for_batch(
          [event_sequences[i] for i in batch_indices],
          [inputs[i] for i in batch_indices],
          initial_state[batch_indices, :],
          temperature)
      final_state[batch_indices, :] = batch_final_state
      softmax[batch_indices] = batch_softmax
      offset += batch_size

    if offset < len(event_sequences):
      # There's an extra non-full batch. Pad it with a bunch of copies of the
      # final sequence.
      num_extra = len(event_sequences) - offset
      pad_size = batch_size - num_extra
      batch_indices = range(offset, len(event_sequences))
      batch_final_state, batch_softmax = self._generate_step_for_batch(
          [event_sequences[i] for i in batch_indices] + [
              copy.deepcopy(event_sequences[-1]) for _ in range(pad_size)],
          [inputs[i] for i in batch_indices] + inputs[-1] * pad_size,
          np.append(initial_state[batch_indices, :],
                    np.tile(inputs[-1, :], (pad_size, 1)),
                    axis=0),
          temperature)
      final_state[batch_indices] = batch_final_state[0:num_extra, :]
      softmax[batch_indices] = batch_softmax[0:num_extra]

    return final_state, softmax

  def _generate_branches(self, event_sequences, loglik, branch_factor,
                         num_steps, inputs, initial_state, temperature):
    """Performs a single iteration of branch generation for beam search.

    This method generates `branch_factor` branches for each event sequence in
    `event_sequences`, where each branch extends the event sequence by
    `num_steps` steps.

    Args:
      event_sequences: A list of event sequence objects.
      loglik: A 1-D numpy array of event sequence log-likelihoods, the same size
          as `event_sequences`.
      branch_factor: The integer branch factor to use.
      num_steps: The integer number of steps to take per branch.
      inputs: A Python list of model inputs, with length equal to the number of
          event sequences.
      initial_state: A numpy array containing the initial RNN states, where
          `initial_state.shape[0]` is equal to the number of event sequences.
      temperature: The softmax temperature.

    Returns:
      all_event_sequences: A list of event sequences, with `branch_factor` times
          as many event sequences as the initial list.
      all_final_state: A numpy array of final RNN states, where
          `final_state.shape[0]` is equal to the length of
          `all_event_sequences`.
      all_loglik: A 1-D numpy array of event sequence log-likelihoods, with
          length equal to the length of `all_event_sequences`.
    """
    all_event_sequences = [copy.deepcopy(events)
                           for events in event_sequences * branch_factor]
    all_inputs = inputs * branch_factor
    all_final_state = np.tile(initial_state, (branch_factor, 1))
    all_loglik = np.tile(loglik, (branch_factor,))

    for _ in range(num_steps):
      all_final_state, all_softmax = self._generate_step(
          all_event_sequences, all_inputs, all_final_state, temperature)
      all_loglik += np.log(all_softmax)

    return all_event_sequences, all_final_state, all_loglik

  def _prune_branches(self, event_sequences, final_state, loglik, k):
    """Prune all but `k` event sequences.

    This method prunes all but the `k` event sequences with highest log-
    likelihood.

    Args:
      event_sequences: A list of event sequence objects.
      final_state: A numpy array containing the final RNN states, where
          `final_state.shape[0]` is equal to the number of event sequences.
      loglik: A 1-D numpy array of log-likelihoods, the same size as
          `event_sequences`.
      k: The number of event sequences to keep after pruning.

    Returns:
      event_sequences: The pruned list of event sequences, of length `k`.
      final_state: The pruned numpy array of final RNN states, where
          `final_state.shape[0]` is equal to `k`.
      loglik: The pruned event sequence log-likelihoods, a 1-D numpy array of
          length `k`.
    """
    indices = heapq.nlargest(k, range(len(event_sequences)),
                             key=lambda i: loglik[i])

    event_sequences = [event_sequences[i] for i in indices]
    final_state = final_state[indices, :]
    loglik = loglik[indices]

    return event_sequences, final_state, loglik

  def _beam_search(self, events, num_steps, temperature, beam_size,
                   branch_factor, steps_per_iteration):
    """Generates an event sequence using beam search.

    Initially, the beam is filled with `beam_size` copies of the initial event
    sequence.

    Each iteration, the beam is pruned to contain only the `beam_size` event
    sequences with highest likelihood. Then `branch_factor` new event sequences
    are generated for each sequence in the beam. These new sequences are formed
    by extending each sequence in the beam by `steps_per_iteration` steps. So
    between a branching and a pruning phase, there will be `beam_size` *
    `branch_factor` active event sequences.

    Prior to the first "real" iteration, an initial branch generation will take
    place. This is for two reasons:

    1) The RNN model needs to be "primed" with the initial event sequence.
    2) The desired total number of steps `num_steps` might not be a multiple of
       `steps_per_iteration`, so the initial branching generates steps such that
       all subsequent iterations can generate `steps_per_iteration` steps.

    After the final iteration, the single event sequence in the beam with
    highest likelihood will be returned.

    Args:
      events: The initial event sequence, a Python list-like object.
      num_steps: The integer length in steps of the final event sequence, after
          generation.
      temperature: A float specifying how much to divide the logits by
         before computing the softmax. Greater than 1.0 makes events more
         random, less than 1.0 makes events less random.
      beam_size: The integer beam size to use.
      branch_factor: The integer branch factor to use.
      steps_per_iteration: The integer number of steps to take per iteration.

    Returns:
      The highest-likelihood event sequence as computed by the beam search.
    """
    event_sequences = [copy.deepcopy(events) for _ in range(beam_size)]
    graph_initial_state = self._session.graph.get_collection('initial_state')[0]
    loglik = np.zeros(beam_size)

    # Choose the number of steps for the first iteration such that subsequent
    # iterations can all take the same number of steps.
    first_iteration_num_steps = (num_steps - 1) % steps_per_iteration + 1

    inputs = self._config.encoder_decoder.get_inputs_batch(
        event_sequences, full_length=True)
    initial_state = np.tile(
        self._session.run(graph_initial_state), (beam_size, 1))
    event_sequences, final_state, loglik = self._generate_branches(
        event_sequences, loglik, branch_factor, first_iteration_num_steps,
        inputs, initial_state, temperature)

    num_iterations = (num_steps -
                      first_iteration_num_steps) / steps_per_iteration

    for _ in range(num_iterations):
      event_sequences, final_state, loglik = self._prune_branches(
          event_sequences, final_state, loglik, k=beam_size)
      inputs = self._config.encoder_decoder.get_inputs_batch(event_sequences)
      event_sequences, final_state, loglik = self._generate_branches(
          event_sequences, loglik, branch_factor, steps_per_iteration, inputs,
          final_state, temperature)

    # Prune to a single sequence.
    event_sequences, final_state, loglik = self._prune_branches(
        event_sequences, final_state, loglik, k=1)

    tf.logging.info('Beam search yields sequence with log-likelihood: %f ',
                    loglik[0])

    return event_sequences[0]

  def _generate_events(self, num_steps, primer_events, temperature=1.0,
                       beam_size=1, branch_factor=1, steps_per_iteration=1):
    """Generate an event sequence from a primer sequence.

    Args:
      num_steps: The integer length in steps of the final event sequence, after
          generation. Includes the primer.
      primer_events: The primer event sequence, a Python list-like object.
      temperature: A float specifying how much to divide the logits by
         before computing the softmax. Greater than 1.0 makes events more
         random, less than 1.0 makes events less random.
      beam_size: An integer, beam size to use when generating event sequences
          via beam search.
      branch_factor: An integer, beam search branch factor to use.
      steps_per_iteration: An integer, number of steps to take per beam search
          iteration.

    Returns:
      The generated event sequence (which begins with the provided primer).

    Raises:
      EventSequenceRnnModelException: If the primer sequence has zero length or
          is not shorter than num_steps.
    """
    if not primer_events:
      raise EventSequenceRnnModelException(
          'primer sequence must have non-zero length')
    if len(primer_events) >= num_steps:
      raise EventSequenceRnnModelException(
          'primer sequence must be shorter than `num_steps`')

    events = primer_events
    if num_steps > len(primer_events):
      events = self._beam_search(events, num_steps - len(events), temperature,
                                 beam_size, branch_factor, steps_per_iteration)
    return events


class EventSequenceRnnConfig(object):
  """Stores a configuration for an event sequence RNN.

  Attributes:
    details: The GeneratorDetails message describing the config.
    encoder_decoder: The EventSequenceEncoderDecoder object to use.
    hparams: The HParams containing hyperparameters to use.
  """

  def __init__(self, details, encoder_decoder, hparams):
    self.details = details
    self.encoder_decoder = encoder_decoder
    self.hparams = hparams
