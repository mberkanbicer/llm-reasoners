import math
from copy import deepcopy
from typing import Generic, Optional, NamedTuple, Callable

import numpy as np

from .. import SearchAlgorithm, WorldModel, SearchConfig, State, Action, Trace, Example


class MCTSNode(Generic[State, Action]):
    def __init__(self, state: Optional[State], action: Optional[Action], parent: "Optional[MCTSNode]" = None,
                 fast_reward: float = 0., fast_reward_aux: dict = {},
                 is_terminal: bool = False, calc_q: Callable[[list[float]], float] = np.mean):
        '''
        A node in the MCTS search tree

        :param state: the current state
        :param action: the action of the last step, i.e., the action from parent node to current node
        :param parent: the parent node, None if root of the tree
        :param prior: an estimation of the reward of the last step
        :param is_terminal: whether the current state is a terminal state
        :param calc_q: the way to calculate the Q value from histories. Defaults: np.mean
        '''
        self.cum_rewards: list[float] = []
        self.fast_reward = self.reward = fast_reward
        self.fast_reward_aux = fast_reward_aux
        self.is_terminal = is_terminal
        self.action = action
        self.state = state
        self.parent = parent
        self.children: 'Optional[list[MCTSNode]]' = None
        self.calc_q = calc_q
        self.depth = 0 if parent is None else parent.depth + 1


    @property
    def Q(self) -> float:
        if self.state is None:
            return self.fast_reward
        else:
            return self.calc_q(self.cum_rewards)



class MCTSResult(NamedTuple):
    terminal_state: State
    cum_reward: float
    trace: Trace
    trace_of_nodes: list[MCTSNode]
    tree_state: MCTSNode
    trace_in_each_iter: list[list[MCTSNode]] = None
    tree_state_after_each_iter: list[MCTSNode] = None


class MCTS(SearchAlgorithm, Generic[State, Action]):
    def __init__(self,
                 output_trace_in_each_iter: bool = False,
                 w_exp: float = 1.,
                 depth_limit: int = 5,
                 n_iter: int = 10,
                 cum_reward: Callable[[list[float]], float] = sum,
                 calc_q: Callable[[list[float]], float] = np.mean,
                 simulate_strategy: str | Callable[[list[float]], int] = 'max',
                 output_strategy: str = 'max_cum_reward',
                 uct_with_fast_reward: bool = True):
        '''
        MCTS algorithm

        :param output_trace: whether to output the trace of the chosen trajectory
        :param output_trace_of_nodes_in_each_iter: whether to output the trace of the chosen trajectory in each iteration ; the trace is *deepcopy*-ed
                                                   will also output *tree_state_after_each_iter*, which is the *deepcopy*-ed root
        :param w_exp: the weight of exploration in UCT
        :param cum_reward: the way to calculate the cumulative reward from each step.
                           The rewards are in *reverse* order, i.e., reward of the last step comes first. Defaults: sum
        :param calc_q: the way to calculate the Q value from histories. Defaults: np.mean
        :param simulate_strategy: simulate strategy. Options: 'max', 'sample', 'random', or use a custom function
        :param output_strategy: the way to output the result. The nodes are not *deepcopy*-ed, so the information is after all iterations
                                Options: 'max_cum_reward': dfs on the final tree to find a trajectory with max reward using :param cum_reward:
                                         'follow_max': starting from root, choose the maximum reward child at each step. May output a non-terminal node if dead end
                                         'max_visit': the terminal node with maximum number of visits
                                         'max_iter': the trajectory with a terminal node and max reward among those in each iterations
                                         'last_iter': the last trajectory. May output a non-terminal node if the last iteration leads to a dead end
                                         'last_terminal_iter': the last trajectory with a terminal node
                                Outputs *None* if no trajectory with terminal node but required
        :param uct_with_fast_reward: if True, use fast_reward instead of reward for unvisited children in UCT
                                     Otherwise, visit the *unvisited* children with maximum fast_reward first
        '''
        super().__init__()
        self.world_model = None
        self.search_config = None
        self.output_trace_in_each_iter = output_trace_in_each_iter
        self.w_exp = w_exp
        self.depth_limit = depth_limit
        self.n_iter = n_iter
        self.cum_reward = cum_reward
        self.calc_q = calc_q
        default_simulate_strategies: dict[str, Callable[[list[float]], int]] = {
            'max': lambda x: np.argmax(x),
            'sample': lambda x: np.random.choice(len(x), p=x),
            'random': lambda x: np.random.choice(len(x)),
        }
        self.simulate_choice: Callable[[list[float]], int] = default_simulate_strategies.get(simulate_strategy, simulate_strategy)
        assert output_strategy in ['max_cum_reward', 'follow_max', 'max_visit', 'max_iter', 'last_iter', 'last_terminal_iter']
        self.output_strategy = output_strategy
        self.uct_with_fast_reward = uct_with_fast_reward
        self._output_iter: list[MCTSNode] = None
        self._output_cum_reward = -math.inf
        self.trace_in_each_iter: list[list[MCTSNode]] = None
        self.root: Optional[MCTSNode] = None

    def iterate(self, node: MCTSNode) -> MCTSNode:
        path = self._select(node)
        if not self._is_terminal_with_depth_limit(path[-1]):
            self._expand(path[-1])
            self._simulate(path)
        cum_reward = self._back_propagate(path)
        if self.output_strategy == 'max_iter' and path[-1].is_terminal and cum_reward > self._max_return:
            self._output_cum_reward = cum_reward
            self._output_iter = path
        if self.output_strategy == 'last_iter':
            self._output_cum_reward = cum_reward
            self._output_iter = path
        if self.output_strategy == 'last_terminal_iter' and path[-1].is_terminal:
            self._output_cum_reward = cum_reward
            self._output_iter = path
        return path

    def _is_terminal_with_depth_limit(self, node: MCTSNode):
        return node.is_terminal or node.depth >= self.depth_limit

    def _select(self, node: MCTSNode) -> list[MCTSNode]:
        path = []
        while True:
            path.append(node)
            if node.children is None or self._is_terminal_with_depth_limit(node):
                return path
            node = self._uct_select(node)
    
    def _uct(self, node: MCTSNode) -> float:
        return node.reward + self.w_exp * np.sqrt(np.log(len(node.parent.cum_rewards)) / max(1, len(node.cum_rewards)))

    def _uct_select(self, node: MCTSNode) -> MCTSNode:
        if self.uct_with_fast_reward:
            return max(node.children, key=self._uct)
        else:
            unvisited_children = filter(lambda x: x.state is None, node.children)
            return max(unvisited_children, key=lambda x: x.fast_reward)
    
    def _expand(self, node: MCTSNode):
        if node.state is None:
            node.state, aux = self.world_model.step(node.parent.state, node.action)
            node.reward = self.search_config.reward(node.state, node.action, **node.fast_reward_aux, **aux)
            node.is_terminal = self.world_model.is_terminal(node.state)
        children = []
        actions = self.search_config.get_actions(node.state)
        for action in actions:
            fast_reward, fast_reward_aux = self.search_config.fast_reward(node.state, action)
            child = MCTSNode(state=None, action=action, parent=node,
                             fast_reward=fast_reward, fast_reward_aux=fast_reward_aux, calc_q=self.calc_q)
            children.append(child)
        node.children = children
    
    def _simulate(self, path: list[MCTSNode]):
        node = path[-1]
        while True:
            if self._is_terminal_with_depth_limit(node):
                return
            if node.children is None:
                self._expand(node)
            if len(node.children) == 0:
                return
            fast_rewards = [child.fast_reward for child in node.children]
            node = node.children[self.simulate_choice(fast_rewards)]

    def _back_propagate(self, path: list[MCTSNode]):
        rewards = []
        cum_reward = -math.inf
        for node in reversed(path):
            rewards.append(node.reward)
            cum_reward = self.cum_reward(rewards)
            node.cum_rewards.append(cum_reward)
        return cum_reward
    
    def _dfs_max_reward(self, path: list[MCTSNode]) -> tuple[float, list[MCTSNode]]:
        cur = path[-1]
        if cur.is_terminal:
            return self.cum_reward(node.reward for node in path[1::-1]), path
        if cur.children is None:
            return -math.inf, path
        visited_children = filter(lambda x: x.state is not None, cur.children)
        if len(visited_children) == 0:
            return -math.inf, path
        return max((self._dfs_max_reward(path + [child]) for child in visited_children), key=lambda x: x[0])

    def search(self):
        self._output_cum_reward = -math.inf
        self._output_iter = None
        self.root = MCTSNode(state=self.world_model.init_state(), action=None, parent=None, calc_q=self.calc_q)
        if self.output_trace_in_each_iter:
            self.trace_of_nodes_in_each_iter = []

        for _ in range(self.n_iter):
            path = self.iterate(self.root)
            if self.output_trace_in_each_iter:
                self.trace_of_nodes_in_each_iter.append(deepcopy(path))

        if self.output_strategy == 'follow_max':
            self._output_iter = []
            cur = self.root
            while True:
                self._output_iter.append(cur)
                if cur.is_terminal:
                    break
                visited_children = filter(lambda x: x.state is not None, cur.children)
                if len(visited_children) == 0:
                    break
                cur = max(visited_children, key=lambda x: x.reward)
            self._output_cum_reward = self.cum_reward(node.reward for node in self._output_iter[1::-1])
        if self.output_strategy == 'max_reward':
            self._output_cum_reward, self._output_iter = self._dfs_max_reward([self.root])
            if self._output_cum_reward == -math.inf:
                self._output_iter = None

    def __call__(self, 
                 world_model: WorldModel[State, Action],
                 search_config: SearchConfig[State, Action],
                 **kwargs) -> MCTSResult:
        self.world_model = world_model
        self.search_config = search_config

        self.search()

        if self._output_iter is None:
            terminal_state = trace = None
        else:
            terminal_state = self._output_iter[-1].state
            trace = [node.state for node in self._output_iter], [node.action for node in self._output_iter[1:]]
        if self.output_trace_in_each_iter:
            trace_in_each_iter = self.trace_in_each_iter
            tree_state_after_each_iter = [trace[0] for trace in trace_in_each_iter]
        else:
            trace_in_each_iter = tree_state_after_each_iter = None
        return MCTSResult(terminal_state=terminal_state,
                          cum_reward=self._output_cum_reward,
                          trace=trace,
                          trace_of_nodes=self._output_iter,
                          tree_state=self.root,
                          trace_in_each_iter=trace_in_each_iter,
                          tree_state_after_each_iter=tree_state_after_each_iter)


'''
class MCTSAggregation(MCTS[State, Action]):
    def __call__(self, init_state: State, output_trace: bool = False) -> State | list[tuple[Action, State]]:
        # TODO: implement aggregate
        pass
'''