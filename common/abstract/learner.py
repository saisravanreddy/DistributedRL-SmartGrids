import time
from abc import ABC, abstractmethod
from collections import deque
from copy import deepcopy
from datetime import datetime
from typing import Union

import numpy as np
import pyarrow as pa
import torch
import torch.nn as nn
import zmq
from zmq.sugar.stopwatch import Stopwatch

import tensorflow as tf
import ray

class Learner(ABC):
    def __init__(
        self, brain: Union[nn.Module, tuple], learner_cfg: dict, comm_cfg: dict
    ):
        self.cfg = learner_cfg
        self.device = self.cfg["learner_device"]
        self.targetupdatefrequency = self.cfg["targetupdatefrequency"]
        self.brain = deepcopy(brain)
        self.replay_data_queue = deque(maxlen=1000)

        # unpack communication configs
        self.param_update_interval = self.cfg["param_update_interval"]
        self.repreq_port = comm_cfg["repreq_port"]
        self.pubsub_port = comm_cfg["pubsub_port"]

        # initialize zmq sockets
        print("[Learner]: initializing sockets..")
        self.initialize_sockets()

    @abstractmethod
    def write_log(self):
        pass

    # @abstractmethod
    # def learning_step(self, data: tuple):
    #     pass

    @abstractmethod
    def get_params(self) -> np.ndarray:
        """Return model params for synchronization"""
        pass

    def params_to_numpy(self, model):
        params = []
        new_model = deepcopy(model)
        state_dict = new_model.cpu().state_dict()
        for param in list(state_dict):
            params.append(state_dict[param].numpy())
        return params

    def initialize_sockets(self):
        # For sending new params to workers
        context = zmq.Context()
        self.pub_socket = context.socket(zmq.PUB)
        self.pub_socket.bind(f"tcp://127.0.0.1:{self.pubsub_port}")

        # For receiving batch from, sending new priorities to Buffer # write another with PUSH/PULL for non PER version
        context = zmq.Context()
        self.rep_socket = context.socket(zmq.REP)
        self.rep_socket.bind(f"tcp://127.0.0.1:{self.repreq_port}")

    def publish_params(self, new_params: np.ndarray):
        new_params_id = pa.serialize(new_params).to_buffer()
        self.pub_socket.send(new_params_id)

    def recv_replay_data_(self):
        replay_data_id = self.rep_socket.recv()
        # print("Learner received replay batch from buffer")
        replay_data = pa.deserialize(replay_data_id)
        self.replay_data_queue.append(replay_data)

    # def send_new_priorities(self, idxes: np.ndarray, priorities: np.ndarray):
    def send_new_priorities(self, idxes: np.ndarray, errors: np.ndarray):
        new_priors = [idxes, errors]
        new_priors_id = pa.serialize(new_priors).to_buffer()
        self.rep_socket.send(new_priors_id)

    def run(self):
        # ray.util.pdb.set_trace() # TODO uncomment for ray debugging
        time.sleep(3)
        tracker = Stopwatch()

        while True:
            self.recv_replay_data_()
            replay_data_all_agents = self.replay_data_queue.pop()

            all_agents_idxs = [[],[],[]]
            all_agents_errors = [[],[],[]]
            for i in range(self.num_of_agents):
                idxs,errors, adl_loss, pricing_loss  = self.agents[i].replay(replay_data_all_agents[i],self.batchsize)
                all_agents_idxs[i] = idxs
                # all_agents_errors[i] = errors
                all_agents_errors[i] = errors.tolist()
                with self.agents[i].pricing_summary_writer.as_default():
                    tf.summary.scalar('Pricing-Loss', float(pricing_loss), self.update_step)
                with self.agents[i].adl_summary_writer.as_default():
                    tf.summary.scalar('ADL-Loss', float(adl_loss), self.update_step)

            # Update target network to prediction network
            if (self.update_step) % (self.targetupdatefrequency) == 0:
                print("target network updated")
                for i in range(self.num_of_agents):
                    self.agents[i].update_target_models()


            self.send_new_priorities(all_agents_idxs,all_agents_errors)

            self.update_step = self.update_step + 1

            if self.update_step % self.param_update_interval == 0:
                params = self.get_params()
                self.publish_params(params)


            # for i in range(self.batchsize):
            #     idx = idxs[i]
            #     self.memory.update(idx, errors[i])

            # replay_data = self.replay_data_queue.pop()
            #
            # for _ in range(self.cfg["multiple_updates"]):
            #     step_info, idxes, priorities = self.learning_step(replay_data)
            #
            #
            # self.send_new_priorities(idxes, priorities)
            #

            # return # TODO comment for multithreading enable