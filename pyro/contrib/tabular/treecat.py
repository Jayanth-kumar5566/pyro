from __future__ import absolute_import, division, print_function

import logging
from collections import deque

import torch

import pyro
import pyro.distributions as dist
from pyro import poutine
from pyro.contrib.autoguide import AutoDelta
from pyro.distributions.spanning_tree import make_complete_graph, sample_tree_mcmc
from pyro.infer import SVI
from pyro.infer.discrete import TraceEnumSample_ELBO, infer_discrete
from pyro.ops.indexing import Vindex
from pyro.optim import Adam


class TreeCat(object):
    """
    The TreeCat model of sparse heterogeneous tabular data.

    :param list features: A ``V``-length list of
        :class:`~pyro.contrib.tabular.features.Feature` objects defining a
        feature model for each column. Feature models can be repeated,
        indicating that two columns share a common feature model (with shared
        learned parameters). Features should reside on the same device as data.
    :param int capacity: Cardinality of latent categorical variables.
    :param torch.LongTensor edges: A ``(V-1, 2)`` shaped tensor representing
        the tree structure. Each of the ``E = (V-1)`` edges is a row ``v1,v2``
        of vertices. Edges must reside on the CPU.
    :param float annealing_rate: The exponential growth rate limit with which
        sufficient statistics approach the full dataset early in training.
        Should be positive.
    """
    def __init__(self, features, capacity=8, edges=None, annealing_rate=0.01):
        V = len(features)
        E = V - 1
        M = capacity
        if edges is None:
            edges = torch.stack([torch.arange(E, device="cpu"),
                                 torch.arange(1, 1 + E, device="cpu")], dim=-1)
        assert capacity > 1
        assert isinstance(edges, torch.LongTensor)  # Note edges must live on CPU.
        assert edges.shape == (E, 2)
        self.features = features
        self.capacity = capacity

        self._feature_guide = AutoDelta(poutine.block(
            self.model, hide_fn=lambda msg: msg["name"].startswith("treecat_")))
        self._edge_guide = _EdgeGuide(capacity=capacity, edges=edges, annealing_rate=annealing_rate)
        self._vertex_prior = torch.full((M,), 0.5)
        self._edge_prior = torch.full((M * M,), 0.5 / M)
        self._saved_z = None

        self.edges = edges

    def __getstate__(self):
        init_args = (self.features, self.capacity, self.edges)
        return {"init_args": init_args, "edge_guide": self._edge_guide}

    def __setstate__(self, state):
        self.__init__(*state["init_args"])
        self._edge_guide = state["edge_guide"]
        self._edge_guide.edges = self.edges

    @property
    def edges(self):
        return self._edges

    @edges.setter
    def edges(self, edges):
        self._edges = edges
        self._edge_guide.edges = edges

        # Construct a directed tree data structures used by ._propagate().
        # The root has no statistical meaning; we choose a root vertex based on
        # computational concerns only, maximizing opportunity for parallelism.
        self._root = find_center_of_tree(edges)
        self._neighbors = [set() for _ in self.features]
        self._edge_index = {}
        for e, (v1, v2) in enumerate(edges.numpy()):
            self._neighbors[v1].add(v2)
            self._neighbors[v2].add(v1)
            self._edge_index[v1, v2] = e
            self._edge_index[v2, v1] = e

    def model(self, data, num_rows=None, impute=False):
        """
        :param list data: batch of heterogeneous column-oriented data.  Each
            column should be either a torch.Tensor (if observed) or None (if
            unobserved).
        :param int num_rows: Optional number of rows in entire dataset, if data
            is is a minibatch.
        :param bool impute: Whether to impute missing features. This should be
            set to False during training and True when making predictions.
        :returns: a copy of the input data, optionally with missing columns
            stochastically imputed according the joint posterior.
        :rtype: list
        """
        assert len(data) == len(self.features)
        assert not all(column is None for column in data)
        device = next(col.device for col in data if col is not None)
        batch_size = next(col.size(0) for col in data if col is not None)
        if num_rows is None:
            num_rows = batch_size
        V = len(self.features)
        E = len(self.edges)
        M = self.capacity

        # Sample a mixture model for each feature.
        mixtures = [None] * V
        components_plate = pyro.plate("components_plate", M, dim=-1)
        for v, feature in enumerate(self.features):
            shared = feature.sample_shared()
            with components_plate:
                mixtures[v] = feature.sample_group(shared)

        # Sample latent vertex- and edge- distributions from a Dirichlet prior.
        with pyro.plate("vertices_plate", V, dim=-1):
            vertex_probs = pyro.sample("treecat_vertex_probs",
                                       dist.Dirichlet(self._vertex_prior.to(device)))
        with pyro.plate("edges_plate", E, dim=-1):
            edge_probs = pyro.sample("treecat_edge_probs",
                                     dist.Dirichlet(self._edge_prior.to(device)))
        if vertex_probs.dim() > 2:
            vertex_probs = vertex_probs.unsqueeze(-3)
            edge_probs = edge_probs.unsqueeze(-3)

        # Sample data-local variables.
        subsample = None if (batch_size == num_rows) else [None] * batch_size
        with pyro.plate("data", num_rows, subsample=subsample, dim=-1):

            # Recursively sample z and x in Markov contexts.
            z = [None] * V
            x = [None] * V
            v = self._root
            self._propagate(data, impute, mixtures, vertex_probs, edge_probs, z, x, v)

        self._saved_z = z
        return x

    @poutine.markov
    def _propagate(self, data, impute, mixtures, vertex_probs, edge_probs, z, x, v):
        # Determine the upstream parent v0 and all downstream children.
        v0 = None
        children = []
        for v2 in self._neighbors[v]:
            if z[v2] is None:
                children.append(v2)
            else:
                v0 = v2

        # Sample discrete latent state from an arbitrarily directed tree structure.
        M = self.capacity
        if v0 is None:
            # Sample root node unconditionally.
            probs = vertex_probs[..., v, :]
        else:
            # Sample node v conditioned on its parent v0.
            joint = edge_probs[..., self._edge_index[v, v0], :]
            joint = joint.reshape(joint.shape[:-1] + (M, M))
            if v0 > v:
                joint = joint.transpose(-1, -2)
            probs = Vindex(joint)[..., z[v0], :]
        z[v] = pyro.sample("treecat_z_{}".format(v), dist.Categorical(probs),
                           infer={"enumerate": "parallel"}).cpu()

        # Sample observed features conditioned on latent classes.
        if data[v] is not None or impute:
            x[v] = pyro.sample("treecat_x_{}".format(v),
                               self.features[v].value_dist(mixtures[v], component=z[v]),
                               obs=data[v])

        # Continue sampling downstream.
        for v2 in children:
            self._propagate(data, impute, mixtures, vertex_probs, edge_probs, z, x, v2)

    def guide(self, data, num_rows=None, impute=False):
        """
        A :class:`~pyro.contrib.autoguide.AutoDelta` guide for MAP inference of
        continuous parameters.
        """
        device = next(col.device for col in data if col is not None)
        V = len(self.features)
        E = V - 1

        self._feature_guide(data, num_rows=num_rows, impute=impute)

        # This guide uses the posterior mean as a point estimate.
        vertex_probs, edge_probs = self._edge_guide.get_posterior()
        with pyro.plate("vertices_plate", V, dim=-1):
            pyro.sample("treecat_vertex_probs",
                        dist.Delta(vertex_probs.to(device), event_dim=1))
        with pyro.plate("edges_plate", E, dim=-1):
            pyro.sample("treecat_edge_probs",
                        dist.Delta(edge_probs.to(device), event_dim=1))

    def impute(self, data, num_samples=None):
        """
        Impute missing columns in data.
        """
        model = self.model
        guide = self.guide
        first_available_dim = -2

        # Optionally draw vectorized samples.
        if num_samples is not None:
            plate = pyro.plate("num_samples_vectorized", num_samples,
                               dim=first_available_dim)
            model = plate(model)
            guide = plate(guide)
            first_available_dim -= 1

        # Sample global parameters from the guide.
        guide_trace = poutine.trace(guide).get_trace(data)
        model = poutine.replay(model, guide_trace)

        # Sample local latent variables using variable elimination.
        model = infer_discrete(model, first_available_dim=first_available_dim)
        return model(data, impute=True)

    def trainer(self, optim=None, backend="python"):
        return TreeCatTrainer(self, optim, backend)


class TreeCatTrainer(object):
    """
    Maintains state to initialize and train a :class:`TreeCat` model.

    :param TreeCat model: A TreeCat model to train.
    :param pyro.optim.optim.PyroOptim optim: A Pyro optimizer to learn feature
        parameters.
    :param str backend: Either "python" or "cpp". Defaults to "python". The
        "cpp" backend is much faster for data with more than ~10 features.
    """
    def __init__(self, model, optim=None, backend="python"):
        assert isinstance(model, TreeCat)
        if optim is None:
            optim = Adam({})
        self._elbo = TraceEnumSample_ELBO(max_plate_nesting=1)
        self._svi = SVI(model.model, model.guide, optim, self._elbo)
        self._model = model
        self.backend = backend

    def init(self, data, init_groups=True):
        assert len(data) == len(self._model.features)
        for feature, column in zip(self._model.features, data):
            if column is not None:
                feature.init(column)
        if init_groups:
            self._elbo.loss(self._model.model, self._model.guide, data)

    def step(self, data, num_rows=None):
        # Perform a gradient optimizer step to learn parameters.
        loss = self._svi.step(data, num_rows=num_rows)

        # Update sufficient statistics in the edge guide.
        self._elbo.sample_saved()
        z = torch.stack(self._model._saved_z)
        self._model._edge_guide.update(num_rows, z)

        # Perform an MCMC step to learn the model.
        model = self._model
        edge_logits = model._edge_guide.compute_edge_logits()
        model.edges = sample_tree_mcmc(edge_logits, model.edges, backend=self.backend)

        return loss


class _EdgeGuide(object):
    """
    Conjugate guide for latent categorical distribution parameters.

    .. note:: This is memory intensive and therefore resides on the CPU.

    :param int capacity: The cardinality of discrete latent variables.
    :param torch.LongTensor edges: A ``(V-1, 2)`` shaped tensor representing
        the tree structure. Each of the ``E = (V-1)`` edges is a row ``v1,v2``
        of vertices.
    :param float annealing_rate: The exponential growth rate limit with which
        sufficient statistics approach the full dataset early in training.
        Should be positive.
    """
    def __init__(self, capacity, edges, annealing_rate):
        assert 0 < annealing_rate
        E = len(edges)
        V = E + 1
        K = V * (V - 1) // 2
        M = capacity
        self.capacity = capacity
        self.edges = edges
        self.annealing_rate = annealing_rate
        self._grid = make_complete_graph(V)

        # Use a Jeffreys prior on vertices, forcing a sparse prior on edges.
        self._vertex_prior = 0.5  # A uniform Dirichlet of shape (M,).
        self._edge_prior = 0.5 / M  # A uniform Dirichlet of shape (M,M).

        # Initialize stats to a single pseudo-observation.
        self._count_stats = 1.
        self._vertex_stats = torch.full((V, M), 1. / M, device="cpu")
        self._complete_stats = torch.full((K, M * M), 1. / M ** 2, device="cpu")

    @torch.no_grad()
    def update(self, num_rows, z):
        """
        Updates count statistics given a minibatch of latent samples ``z``.

        :param int num_rows: Size of the complete dataset.
        :param torch.Tensor z: A minibatch of latent variables of size
            ``(V, batch_size)``.
        """
        assert z.dim() == 2
        M = self.capacity
        batch_size = z.size(-1)
        if num_rows is None:
            num_rows = batch_size

        # Early in learning, we limit stats accumulation to slow exponential
        # growth determined by annealing_rate. Later in learning we
        # exponentially smooth batches to approximate the entire dataset.
        assert batch_size <= num_rows
        assert self._count_stats > 0
        annealing = (1 + self.annealing_rate) / (1 + batch_size / self._count_stats)
        exponential_smoothing = 1 / (1 + batch_size / num_rows)
        decay = min(annealing, exponential_smoothing)

        # Accumulate statistics and decay.
        self._count_stats += batch_size
        self._count_stats *= decay
        one = self._vertex_stats.new_tensor(1.)
        self._vertex_stats.scatter_add_(-1, z, one.expand_as(z))
        self._vertex_stats *= decay
        zz = (M * z)[self._grid[0]] + z[self._grid[1]]
        self._complete_stats.scatter_add_(-1, zz, one.expand_as(zz))
        self._complete_stats *= decay

        # Log metrics to diagnose convergence issues.
        if logging.Logger(None).isEnabledFor(logging.DEBUG):
            logging.debug("count_stats = {:0.1f}, batch_size = {}, num_rows = {}".format(
                self._count_stats, batch_size, num_rows))

            vertex_probs, edge_probs = self.get_posterior()
            vertex_entropy = -(vertex_probs * vertex_probs.log()).sum(-1)
            perplexity = vertex_entropy.exp().sort(descending=True)[0]
            perplexity = ["{: >4.1f}".format(p) for p in perplexity]
            logging.debug(" ".join(["perplexity:"] + perplexity))

            edge_entropy = -(edge_probs * edge_probs.log()).sum(-1)
            mutual_info = vertex_entropy[self.edges].sum(-1) - edge_entropy
            mutual_info = mutual_info.sort(descending=True)[0]
            mutual_info = ["{: >4.1f}".format(i) for i in mutual_info]
            logging.debug(" ".join(["mutual_info:"] + mutual_info))

    @torch.no_grad()
    def get_posterior(self):
        """
        Computes posterior mean under a Dirichlet prior.

        :returns: a pair ``vetex_probs,edge_probs`` with the posterior mean
            probabilities of each of the ``V`` latent variables and pairwise
            probabilities of each of the ``K=V*(V-1)/2`` pairs of latent
            variables.
        :rtype: tuple
        """
        v1, v2 = self.edges.t()
        k = v1 + v2 * (v2 - 1) // 2
        edge_stats = self._complete_stats[k]

        vertex_probs = self._vertex_prior + self._vertex_stats
        vertex_probs /= vertex_probs.sum(-1, True)
        edge_probs = self._edge_prior + edge_stats
        edge_probs /= edge_probs.sum(-1, True)
        return vertex_probs, edge_probs

    @torch.no_grad()
    def compute_edge_logits(self):
        """
        Computes a non-normalized log likelihoods of each of the
        ``K=V*(V-1)/2`` edges in the complete graph. This can be used to learn
        tree structure distributed according to the
        :class:`~pyro.distributions.SpanningTree` distribution.

        :returns: a ``(K,)``-shaped tensor of edges logits.
        :rtype: torch.Tensor
        """
        E = len(self.edges)
        V = E + 1
        K = V * (V - 1) // 2
        vertex_logits = _dirmul_log_prob(self._vertex_prior, self._vertex_stats)
        edge_logits = _dirmul_log_prob(self._edge_prior, self._complete_stats)
        edge_logits -= vertex_logits[self._grid[0]]
        edge_logits -= vertex_logits[self._grid[1]]
        assert edge_logits.shape == (K,)
        return edge_logits


def _dirmul_log_prob(alpha, counts):
    """
    Computes non-normalized log probability of a Dirichlet-multinomial
    distribution in a numerically stable way. Equivalent to::

        (alpha + counts).lgamma().sum(-1) - (1 + counts).lgamma().sum(-1)
    """
    assert isinstance(alpha, float)
    shape = counts.shape
    temp = (counts.unsqueeze(-1) + counts.new_tensor([alpha, 1])).lgamma_()
    temp = temp.reshape(-1, 2).mv(temp.new_tensor([1., -1.])).reshape(shape)
    return temp.sum(-1)


def find_center_of_tree(edges):
    """
    Finds a maximally central vertex in a tree.

    :param torch.LongTensor edges: A ``(V-1, 2)`` shaped tensor representing
        the tree structure. Each of the ``E = (V-1)`` edges is a row ``v1,v2``
        of vertices.
    :returns: Vertex id of a maximally central vertex.
    :rtype: int
    """
    V = len(edges) + 1
    neighbors = [set() for _ in range(V)]
    for v1, v2 in edges.numpy():
        neighbors[v1].add(v2)
        neighbors[v2].add(v1)
    queue = deque(v for v in range(V) if len(neighbors[v]) <= 1)
    while queue:
        v = queue.popleft()
        for v2 in sorted(neighbors[v]):
            neighbors[v2].remove(v)
            if len(neighbors[v2]) == 1:
                queue.append(v2)
    return v


def print_tree(edges, feature_names, root=None):
    """
    Returns a text representation of the feature tree.

    :param torch.LongTensor edges: A ``(V-1, 2)`` shaped tensor representing
        the tree structure. Each of the ``E = (V-1)`` edges is a row ``v1,v2``
        of vertices.
    :param list feature_names: A list of feature names.
    :param str root: The name of the root feature (optional).
    :returns: A text representation of the tree with one feature per line.
    :rtype: str
    """
    assert len(feature_names) == 1 + len(edges)
    if root is None:
        root = feature_names[find_center_of_tree(edges)]
    assert root in feature_names
    neighbors = [set() for _ in feature_names]
    for v1, v2 in edges.numpy():
        neighbors[v1].add(v2)
        neighbors[v2].add(v1)
    stack = [feature_names.index(root)]
    seen = set(stack)
    lines = []
    while stack:
        backtrack = True
        for neighbor in sorted(neighbors[stack[-1]], reverse=True):
            if neighbor not in seen:
                seen.add(neighbor)
                stack.append(neighbor)
                backtrack = False
                break
        if backtrack:
            name = feature_names[stack.pop()]
            lines.append((len(stack), name))
    lines.reverse()
    return "\n".join(["{}{}".format("  " * i, n) for i, n in lines])