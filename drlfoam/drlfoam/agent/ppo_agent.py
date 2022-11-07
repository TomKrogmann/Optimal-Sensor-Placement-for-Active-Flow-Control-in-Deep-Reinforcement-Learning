
from typing import List
from collections import defaultdict
import pickle
import torch as pt
from .agent import Agent, FCPolicy, FCValue, compute_gae, compute_returns
from ..constants import EPS_SP, DEFAULT_TENSOR_TYPE


pt.set_default_tensor_type(DEFAULT_TENSOR_TYPE)

DEFAULT_FC_DICT = {
    "n_layers": 2,
    "n_neurons": 512,
    "activation": pt.nn.functional.relu
}


class PPOAgent(Agent):
    def __init__(self, n_states, n_actions, action_min, action_max,
                 policy_dict: dict = DEFAULT_FC_DICT,
                 policy_epochs: int = 100,
                 policy_lr: float = 0.001,
                 policy_clip: float = 0.2,
                 policy_grad_norm: float = float("inf"),
                 policy_kl_stop: float = 0.2,
                 value_dict: dict = DEFAULT_FC_DICT,
                 value_epochs: int = 100,
                 value_lr: float = 0.001,
                 value_clip: float = 0.2,
                 value_grad_norm: float = float("inf"),
                 value_mse_stop: float = 25.0,
                 gamma: float = 0.99,
                 lam: float = 0.97,
                 entropy_weight: float = 0.01
                 ):
        self._n_states = n_states
        self._n_actions = n_actions
        self._action_min = action_min
        self._action_max = action_max
        self._policy_epochs = policy_epochs
        self._policy_lr = policy_lr
        self._policy_clip = policy_clip
        self._policy_grad_norm = policy_grad_norm
        self._policy_kl_stop = policy_kl_stop
        self._value_epochs = value_epochs
        self._value_lr = value_lr
        self._value_clip = value_clip
        self._value_grad_norm = value_grad_norm
        self._value_mse_stop = value_mse_stop
        self._gamma = gamma
        self._lam = lam
        self._entropy_weight = entropy_weight

        # networks and optimizers
        self._policy = FCPolicy(self._n_states, self._n_actions, self._action_min,
                                self._action_max, **policy_dict)
        self._policy_optimizer = pt.optim.Adam(
            self._policy.parameters(), lr=self._policy_lr
        )
        self._value = FCValue(self._n_states, **value_dict)
        self._value_optimizer = pt.optim.Adam(
            self._value.parameters(), lr=self._value_lr
        )

        # history
        self._history = defaultdict(list)

    def update(self, states: List[pt.Tensor], actions: List[pt.Tensor],
               rewards: List[pt.Tensor]):

        values = [self._value(s).detach() for s in states]
        # compute log_p for all but the final experience tuple
        log_p_old = pt.cat([self._policy.predict(s[:-1], a[:-1])[0].detach() for s, a in zip(states, actions)])
        returns = pt.cat([compute_returns(r, self._gamma) for r in rewards])
        gaes = pt.cat([compute_gae(r, v, self._gamma, self._lam) for r, v in zip(rewards, values)])
        gaes = (gaes - gaes.mean()) / (gaes.std() + EPS_SP)
        values = pt.cat(values)
        # create tensors with all but the final state/action of each trajectory for convenience
        states_wf = pt.cat([s[:-1] for s in states])
        actions_wf = pt.cat([a[:-1] for a in actions])

        # policy update
        p_loss_, e_loss_, kl_ = [], [], []
        for e in range(self._policy_epochs):

            # compute loss and update weights
            log_p_new, entropy = self._policy.predict(states_wf, actions_wf)
            p_ratio = (log_p_new - log_p_old).exp()
            policy_objective = gaes * p_ratio
            policy_objective_clipped = gaes * \
                p_ratio.clamp(1.0 - self._policy_clip, 1.0 + self._policy_clip)
            policy_loss = -pt.min(policy_objective, policy_objective_clipped).mean()
            entropy_loss = -entropy.mean() * self._entropy_weight
            self._policy_optimizer.zero_grad()
            (policy_loss + entropy_loss).backward()
            pt.nn.utils.clip_grad_norm_(self._policy.parameters(), self._policy_grad_norm)
            self._policy_optimizer.step()
            p_loss_.append(policy_loss.item())
            e_loss_.append(entropy_loss.item())

            # check KL-divergence
            with pt.no_grad():
                log_p, _ = self._policy.predict(states_wf, actions_wf)
                kl = (log_p_old - log_p).mean()
                kl_.append(kl.item())
                if kl.item() > self._policy_kl_stop:
                    print(f"Stopping policy training after {e} epochs due to KL-criterion.")
                    break

        # value update
        v_loss_, mse_ = [], []
        for e in range(self._value_epochs):
            # compute loss and update weights
            values_new = self._value(pt.cat(states))
            values_new_clipped = values + (values_new - values).clamp(
                -self._value_clip, self._value_clip
            )
            v_loss = (returns - values_new).pow(2)
            v_loss_clipped = (returns - values_new_clipped).pow(2)
            value_loss = pt.max(v_loss, v_loss_clipped).mul(0.5).mean()
            self._value_optimizer.zero_grad()
            value_loss.backward()
            pt.nn.utils.clip_grad_norm_(self._value.parameters(), self._value_grad_norm)
            self._value_optimizer.step()
            v_loss_.append(value_loss.item())

            # check difference to old values
            with pt.no_grad():
                values_check = self._value(pt.cat(states))
                mse = (values - values_check).pow(2).mul(0.5).mean()
                mse_.append(mse.item())
                if mse.item() > self._value_mse_stop:
                    print(f"Stopping value training after {e} epochs due to MSE-criterion.")
                    break

        # save history
        self._history["policy_loss"].append(p_loss_)
        self._history["entropy_loss"].append(e_loss_)
        self._history["policy_div"].append(kl_)
        self._history["value_loss"].append(v_loss_)
        self._history["value_mse"].append(mse_)

    def save(self, policy_path: str, value_path: str):
        with open(policy_path, "wb") as pf:
            pickle.dump(self._policy, pf, protocol=pickle.HIGHEST_PROTOCOL)
        with open(value_path, "wb") as vf:
            pickle.dump(self._value, vf, protocol=pickle.HIGHEST_PROTOCOL)

    def load(self, policy_path: str, value_path: str):
        with open(policy_path, "rb") as pf:
            self._policy = pickle.load(pf)
        with open(value_path, "rb") as vf:
            self._value = pickle.load(vf)

    def trace_policy(self):
        return pt.jit.script(self._policy)

    @property
    def history(self) -> dict:
        return self._history
