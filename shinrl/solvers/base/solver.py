"""
Author: Toshinori Kitamura
Affiliation: NAIST & OSX
"""
from __future__ import annotations

import inspect
import random
from abc import ABC, abstractmethod, abstractstaticmethod
from itertools import count
from typing import Dict, Iterator, List, Optional, Type

import gym
import jax
import numpy as np
import structlog
from chex import PRNGKey
from tqdm import tqdm

from shinrl import ShinEnv

from .core.config import SolverConfig
from .core.history import History


class Solver(ABC, History):
    """
    Base class to implement solvers. The results are treated by the inherited History class.

    # MixIn Design:
    Our Solver interface adopts "mixin" design to realize the flexible behavior.
    The `make_mixin` method should return mixins that have necessary methods such as `evaluate` and `step` functions.
    See [shinrl/solvers/vi/discrete/solver.py] for an example implementation.
    """

    _id: Iterator[int] = count(0)
    DefaultConfig = SolverConfig

    @staticmethod
    def factory(
        env: gym.Env,
        config: SolverConfig,
        mixins: List[Type[object]],
    ) -> Solver:
        """Instantiate a solver with mixins and initialize it."""

        class MixedSolver(*mixins):
            pass

        solver = MixedSolver()
        solver.initialize(env, config)
        return solver

    @abstractstaticmethod
    def make_mixins(env: gym.Env, config: SolverConfig) -> List[Type[object]]:
        """Make a list of mixins from env and config"""
        pass

    @abstractmethod
    def evaluate(self) -> Dict[str, float]:
        """Evaluate the solver and return the dict of results. Called every self.config.eval_interval steps."""
        pass

    @abstractmethod
    def step(self) -> None:
        """Execute the solver by one step."""
        pass

    def __init__(self) -> None:
        self.env_id: int = -1
        self.solver_id: str = f"{type(self).__name__}-{next(self._id)}"
        self.logger = structlog.get_logger(solver_id=self.solver_id, env_id=None)
        self.is_initialized: bool = False
        self.env = None
        self.key: PRNGKey = None

    def initialize(
        self,
        env: gym.Env,
        config: Optional[SolverConfig] = None,
    ) -> None:
        """Set the env and initialize the history.
        Args:
            env (gym.Env): Environment to solve..
            config (SolverConfig, optional): Configuration of an algorithm.
        """

        self.init_history()
        self.set_config(config)
        self.set_env(env)
        self.seed(self.config.seed)
        self.is_initialized = True

        inheritances = inspect.getmro(self.__class__)
        mixins = [mixin.__name__ for mixin in inheritances if ("MixIn" in str(mixin))]
        methods = inspect.getmembers(self, predicate=inspect.ismethod)
        methods = [method[1].__qualname__ for method in methods]
        self.logger.info("Solver is initialized.", mixins=mixins, methods=methods)

    def seed(self, seed: int = 0) -> None:
        self.key = jax.random.PRNGKey(seed)
        self.env.seed(seed)
        random.seed(seed)
        np.random.seed(seed)

    @property
    def is_shin_env(self) -> bool:
        if isinstance(self.env, gym.Wrapper):
            return isinstance(self.env.unwrapped, ShinEnv)
        else:
            return isinstance(self.env, ShinEnv)

    def set_env(self, env: gym.Env, reset: bool = True) -> None:
        """Set the environment to solve.
        Args:
            env (gym.Env): Environment to solve.
            reset (bool): Reset the env if True
        """
        self.env = env
        if self.is_shin_env:
            if self.config.discount != env.config.discount:
                self.logger.warning(
                    f"env.config.discount != solver.config.discount ({env.config.discount} != {self.config.discount}). \
                    This may cause an unexpected behavior."
                )
            self.dS, self.dA, self.horizon = env.mdp.dS, env.mdp.dA, env.config.horizon

        if reset:
            if isinstance(self.env, gym.wrappers.Monitor):
                # With Monitor, reset() cannot be called unless the episode is over.
                if self.env.stats_recorder.steps is None:
                    self.env.obs = self.env.reset()
                else:
                    done = False
                    while not done:
                        _, _, done, _ = self.env.step(self.env.action_space.sample())
                    self.env.obs = self.env.reset()
            else:
                self.env.obs = self.env.reset()
        else:
            assert hasattr(
                env, "obs"
            ), 'env must have attribute "obs". Do env.obs = obs before calling "set_env".'
        self.env_id += 1
        self.logger = structlog.get_logger(solver_id=self.solver_id, env_id=self.env_id)
        self.logger.info("set_env is called.")

    def run(self) -> None:
        """
        Run the solver with the step function.
        Call self.evaluate() every [eval_interval] steps.
        """

        assert self.is_initialized, '"self.initialize" is not called.'
        num_steps = self.config.steps_per_epoch
        for _ in tqdm(range(num_steps), desc=f"Epoch {self.n_epoch}"):
            if self.n_step % self.config.eval_interval == 0:
                res = self.evaluate()
                for key, val in res.items():
                    self.add_scalar(key, val)
            self.step()
            self.n_step += 1
        self.n_epoch += 1
        self.logger.info(
            f"Epoch {self.n_epoch} has ended.",
            epoch_summary=self.recent_summary(num_steps),
            tb_dict=list(self.tb_dict.keys()),
            prms_dict=list(self.prms_dict.keys()),
        )
