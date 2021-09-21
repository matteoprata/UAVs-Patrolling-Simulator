
import numpy as np
from src.utilities import config, utilities as util
from src.nn.A2C import A2C

import torch


class PatrollingA2C:
    def __init__(self,
                 pretrained_model_path,
                 n_actions,
                 n_features,
                 n_hidden_neurons_lv1,
                 n_hidden_neurons_lv2,
                 n_hidden_neurons_lv3,
                 simulator,
                 metrics,
                 batch_size=32,
                 lr=0.0001,
                 discount_factor=.99,
                 replay_memory_depth=100000,
                 swap_models_every_decision=500,
                 is_load_model=True,
                 ):

        self.simulator = simulator
        self.metrics = metrics
        self.batch_size = batch_size
        self.device = "cpu"
        self.lr = lr

        # number of actions, actions, number of states
        self.n_actions = n_actions
        self.n_features = n_features
        self.n_hidden_neurons_lv1 = n_hidden_neurons_lv1
        self.n_hidden_neurons_lv2 = n_hidden_neurons_lv2
        self.n_hidden_neurons_lv3 = n_hidden_neurons_lv3
        self.n_decision_step = 0

        # learning parameters
        self.discount_factor = discount_factor
        self.epsilon_decay = self.compute_epsilon_decay()
        self.replay_memory = util.LimitedList(replay_memory_depth)
        self.swap_models_every_decision = swap_models_every_decision

        # make the simulation reproducible
        np.random.seed(self.simulator.sim_seed)
        # tf.set_random_seed(self.simulator.sim_seed)
        self.is_load_model = is_load_model
        self.current_loss = 0

        # build neural models
        if not self.is_load_model:
            self.model = A2C(self.n_features,
                             self.n_hidden_neurons_lv1,
                             self.n_hidden_neurons_lv2,
                             self.n_hidden_neurons_lv3,
                             self.n_actions)

            self.optimizer = torch.optim.Adam(self.model.parameters(), lr=self.lr)
        else:
            self.model = torch.load(pretrained_model_path)

        # TO DECLARE ABOVE
        self.saved_actions = []
        self.rewards = []
        self.dones = []

    def compute_epsilon_decay(self, zero_perc_simulation=config.EXPLORE_PORTION, prob_threshold=config.ZERO_TOLERANCE):
        # keep the experience > .0001 until the first %80 of the steps
        # e^(- step_with_zero_exp * epsilon_decay) = 10^-4 -> - step_with_zero_exp * epsilon_decay = log_e 10^-4
        sim_duration = self.simulator.episode_duration * self.simulator.n_episodes * self.simulator.n_epochs
        step_with_zero_exp = sim_duration * zero_perc_simulation
        return - np.log(prob_threshold) / step_with_zero_exp

    @staticmethod
    def explore_probability(step, exp_coeff, base=np.e):
        return base ** (-step*exp_coeff)

    def decay(self):
        """ Probability of exploration now. """
        explore_prob = self.explore_probability(self.simulator.cur_step_total, self.epsilon_decay)
        return explore_prob

    def flip_biased_coin(self, p):
        """ Return true with probability p, false with probability 1-p. """
        return self.simulator.rnd_explore.random() < p

    def is_explore_probability(self):
        """ Returns True if it is time to explore, False otherwise. """
        return self.flip_biased_coin(self.decay())

    def predict(self, state, is_explore=True, forced_action=None, forbidden_action=None):
        """  Given an input state, it returns the action predicted by the model if no exploration is done
          and the model is given as an input, if the exploration goes through. """

        if self.is_load_model:
            is_explore = False

        # state = np.asarray(state).astype(np.float32)
        state = torch.tensor(state).double().to(self.device)
        probs, state_value = self.model(state)

        if forced_action is not None:
            probs = self.do_force_action(probs, forced_action)
        elif forbidden_action is not None:
            probs = self.do_forbid_action(probs, forbidden_action)

        actions_distribution = torch.distributions.Categorical(probs)
        action = actions_distribution.sample()

        # assert(forbidden_action != action.item())

        self.saved_actions.append((actions_distribution.log_prob(action), state_value))
        return action.item()

    # UTILITIES

    def do_force_action(self, distribution, action):
        distribution.data[:] = 0
        distribution.data[action] = 1
        return distribution

    def do_forbid_action(self, distribution, action):
        to_distribute = distribution.data[action] / (self.n_actions-1)
        distribution.data[action] = 0
        distribution.data[:action] += to_distribute
        distribution.data[action+1:] += to_distribute
        # assert the sum
        return distribution

    def train(self, s_prime):

        if self.is_load_model:
            return

        # if len(self.rewards) == self.batch_size: # 
        # True if it is the last training possible in this episode

        on_flight = (self.simulator.cur_step + 1) + np.ceil(config.DELTA_DEC / self.simulator.ts_duration_sec) >= self.simulator.episode_duration
        on_target = s_prime.is_final

        if on_target:
            # print("It is", self.simulator.episode_duration, "time for a new training.")

            # TODO check if this R should be instead value of next state
            R = 0
            policy_losses = []  # list to save actor (policy) loss
            value_losses = []   # list to save critic (value) loss
            returns = []         # list to save the true values

            # machine smallest number
            eps = np.finfo(np.float32).eps.item()

            # RETURNS
            # calculate the true value using rewards returned from the environment
            for irev in reversed(range(len(self.rewards))):
                # calculate the discounted value
                # DONE TODO add mask as in https://github.com/yc930401/Actor-Critic-pytorch/blob/master/Actor-Critic.py
                R = self.rewards[irev] + self.discount_factor * R  # * self.dones[irev]
                returns.insert(0, R)

            # normalization
            returns = torch.tensor(returns)
            returns = (returns - returns.mean()) / (returns.std() + eps)

            for (log_prob, value), ret in zip(self.saved_actions, returns):
                # RET is the truth
                advantage = ret - value.item()

                # calculate actor (policy) loss
                policy_losses.append(-log_prob * advantage)

                # calculate critic (value) loss using L1 smooth loss
                value_losses.append(torch.nn.functional.smooth_l1_loss(value, torch.tensor([ret])))

            # reset gradients
            self.optimizer.zero_grad()

            # sum up all the values of policy_losses and value_losses
            loss = torch.stack(policy_losses).sum() + torch.stack(value_losses).sum()
            self.current_loss = loss.item()

            # perform backprop
            loss.backward()
            self.optimizer.step()

            # reset rewards and action buffer
            del self.saved_actions[:]
            del self.rewards[:]

            return self.current_loss

    def save_model(self, fname):
        torch.save(self.model, fname)



