import sys
import torch
import torch.optim as optim
import torch.nn as nn

import numpy as np
import pandas as pd
import seaborn as sns
import matplotlib
import matplotlib.pyplot as plt
from .spherical_harmonics import RealSphericalHarmonics

from .networks import FCNN
from inspect import signature
from copy import deepcopy


def _nn_output_spherical_input(net, rs, thetas, phis):
    points = torch.cat((rs, thetas, phis), 1)
    return net(points)


class BaseConditionSpherical:
    def enforce(self, net, r, theta, phi):
        raise NotImplementedError(f"Abstract {self.__class__.__name__} cannot be enforced")


class NoConditionSpherical(BaseConditionSpherical):
    def __init__(self):
        pass

    def enforce(self, net, r, theta, phi):
        return _nn_output_spherical_input(net, r, theta, phi)


class BaseGenerator:
    def __init__(self, *args):
        self.size = ...
        raise NotImplementedError(f"Abstract class {self.__class__.__name__} cannot be instantiated")

    def get_examples(self):
        raise NotImplementedError(f"method of abstract class {self.__class__.__name__} cannot be called")


class ExampleGenerator3D(BaseGenerator):
    """An example generator for generating 3-D training points. NOT TO BE CONFUSED with `ExampleGeneratorSpherical`
        :param grid: The discretization of the 3 dimensions, if we want to generate points on a :math:`m \\times n \\times k` grid, then `grid` is `(m, n, k)`, defaults to `(10, 10, 10)`.
        :type grid: tuple[int, int, int], optional
        :param xyz_min: The lower bound of 3 dimensions, if we only care about :math:`x \\geq x_0`, :math:`y \\geq y_0`, and :math:`z \\geq z_0` then `xyz_min` is `(x_0, y_0, z_0)`, defaults to `(0.0, 0.0, 0.0)`.
        :type xyz_min: tuple[float, float, float], optional
        :param xyz_max: The upper bound of 3 dimensions, if we only care about :math:`x \\leq x_1`, :math:`y \\leq y_1`, and :math:`z \\leq z_1` then `xyz_max` is `(x_1, y_1, z_1)`, defaults to `(1.0, 1.0, 1.0)`.
        :type xyz_max: tuple[float, float, float], optional
        :param method: The distribution of the 3-D points generated. If set to 'equally-spaced', the points will be fixed to the grid specified. If set to 'equally-spaced-noisy', a normal noise will be added to the previously mentioned set of points, defaults to 'equally-spaced-noisy'.
        :type method: str, optional
        :raises ValueError: When provided with an unknown method.
    """

    def __init__(self, grid=(10, 10, 10), xyz_min=(0.0, 0.0, 0.0), xyz_max=(1.0, 1.0, 1.0),
                 method='equally-spaced-noisy'):
        r"""Initializer method

        .. note::
            A instance method `get_examples` is dynamically created to generate 2-D training points. It will be called by the function `solve2D`.
        """
        self.size = grid[0] * grid[1] * grid[2]

        x = torch.linspace(xyz_min[0], xyz_max[0], grid[0], requires_grad=True)
        y = torch.linspace(xyz_min[1], xyz_max[1], grid[1], requires_grad=True)
        z = torch.linspace(xyz_min[2], xyz_max[2], grid[2], requires_grad=True)
        grid_x, grid_y, grid_z = torch.meshgrid(x, y, z)
        self.grid_x, self.grid_y, self.grid_z = grid_x.flatten(), grid_y.flatten(), grid_z.flatten()

        def trunc(tensor, min, max):
            tensor[tensor < min] = min
            tensor[tensor > max] = max

        if method == 'equally-spaced':
            self.get_examples = lambda: (self.grid_x, self.grid_y, self.grid_z)
        elif method == 'equally-spaced-noisy':
            self.noise_xmean = torch.zeros(self.size)
            self.noise_ymean = torch.zeros(self.size)
            self.noise_zmean = torch.zeros(self.size)
            self.noise_xstd = torch.ones(self.size) * ((xyz_max[0] - xyz_min[0]) / grid[0]) / 4.0
            self.noise_ystd = torch.ones(self.size) * ((xyz_max[1] - xyz_min[1]) / grid[1]) / 4.0
            self.noise_zstd = torch.ones(self.size) * ((xyz_max[2] - xyz_min[2]) / grid[2]) / 4.0
            self.get_examples = lambda: (
                trunc(self.grid_x + torch.normal(mean=self.noise_xmean, std=self.noise_xstd), xyz_min[0], xyz_max[0]),
                trunc(self.grid_y + torch.normal(mean=self.noise_ymean, std=self.noise_ystd), xyz_min[1], xyz_max[1]),
                trunc(self.grid_z + torch.normal(mean=self.noise_zmean, std=self.noise_zstd), xyz_min[2], xyz_max[2]),
            )
        else:
            raise ValueError(f'Unknown method: {method}')


class ExampleGeneratorSpherical(BaseGenerator):
    """An example generator for generating points in spherical coordinates. NOT TO BE CONFUSED with `ExampleGenerator3D`
    :param size: number of points in 3-D sphere
    :type size: int
    :param r_min: radius of the interior boundary
    :type r_min: float, optional
    :param r_max: radius of the exterior boundary
    :type r_max: float, optional
    :param method: The distribution of the 3-D points generated. If set to 'equally-radius-noisy', radius of the points will be drawn from a uniform distribution :math:`r \\sim U[r_{min}, r_{max}]`. If set to 'equally-spaced-noisy', squared radius of the points will be drawn from a uniform distribution :math:`r^2 \\sim U[r_{min}^2, r_{max}^2]`
    :type method: str, optional
    """

    def __init__(self, size, r_min=0., r_max=1., method='equally-spaced-noisy'):
        if r_min < 0 or r_max < r_min:
            raise ValueError(f"Illegal range [f{r_min}, {r_max}]")

        if method == 'equally-spaced-noisy':
            lower = r_min ** 2
            upper = r_max ** 2
            rng = upper - lower
            self.get_r = lambda: torch.sqrt(rng * torch.rand(self.shape) + lower)
        elif method == "equally-radius-noisy":
            lower = r_min
            upper = r_max
            rng = upper - lower
            self.get_r = lambda: rng * torch.rand(self.shape) + lower
        else:
            raise ValueError(f'Unknown method: {method}')

        self.size = size  # stored for `solve_spherical_system` to access
        self.shape = (size,)  # used for `self.get_example()`

    def get_examples(self):
        a = torch.rand(self.shape)
        b = torch.rand(self.shape)
        c = torch.rand(self.shape)
        denom = a + b + c
        # `x`, `y`, `z` here are just for computation of `theta` and `phi`
        epsilon = 1e-6
        x = torch.sqrt(a / denom) + epsilon
        y = torch.sqrt(b / denom) + epsilon
        z = torch.sqrt(c / denom) + epsilon
        # `sign_x`, `sign_y`, `sign_z` are either -1 or +1
        sign_x = torch.randint(0, 2, self.shape, dtype=x.dtype) * 2 - 1
        sign_y = torch.randint(0, 2, self.shape, dtype=y.dtype) * 2 - 1
        sign_z = torch.randint(0, 2, self.shape, dtype=z.dtype) * 2 - 1

        x = x * sign_x
        y = y * sign_y
        z = z * sign_z

        theta = torch.acos(z).requires_grad_(True)
        phi = -torch.atan2(y, x) + np.pi  # atan2 ranges (-pi, pi] instead of [0, 2pi)
        phi.requires_grad_(True)
        r = self.get_r().requires_grad_(True)

        return r, theta, phi


class EnsembleExampleGenerator(BaseGenerator):
    r"""
    An ensemble generator for sampling points, whose `get_example` returns all the samples of its sub-generators
    :param \*generators: a sequence of sub-generators, must have a .size field and a .get_examples() method
    """

    def __init__(self, *generators):
        self.generators = generators
        self.size = sum(gen.size for gen in generators)

    def get_examples(self):
        all_examples = [gen.get_examples() for gen in self.generators]
        # zip(*sequence) is just `unzip`ping a sequence into sub-sequences, refer to this post for more
        # https://stackoverflow.com/questions/19339/transpose-unzip-function-inverse-of-zip
        segmented = zip(*all_examples)
        return [torch.cat(seg) for seg in segmented]


class DirichletBVPSpherical(BaseConditionSpherical):
    """Dirichlet boundary condition for the interior and exterior boundary of the sphere, where the interior boundary is not necessarily a point
        We are solving :math:`u(t)` given :math:`u(r, \\theta, \\phi)\\bigg|_{r = r_0} = f(\\theta, \\phi)` and :math:`u(r, \\theta, \\phi)\\bigg|_{r = r_1} = g(\\theta, \\phi)`

    :param r_0: The radius of the interior boundary. When r_0 = 0, the interior boundary is collapsed to a single point (center of the ball)
    :type r_0: float
    :param f: The value of :math:u on the interior boundary. :math:`u(r, \\theta, \\phi)\\bigg|_{r = r_0} = f(\\theta, \\phi)`.
    :type f: function
    :param r_1: The radius of the exterior boundary; if set to None, `g` must also be None
    :type r_1: float or None
    :param g: The value of :math:u on the exterior boundary. :math:`u(r, \\theta, \\phi)\\bigg|_{r = r_1} = g(\\theta, \\phi)`. If set to None, `r_1` must also be set to None
    :type g: function or None
    """

    def __init__(self, r_0, f, r_1=None, g=None):
        """Initializer method
        """
        if (r_1 is None) ^ (g is None):
            raise ValueError(f'r_1 and g must be both/neither set to None; got r_1={r_1}, g={g}')
        self.r_0, self.r_1 = r_0, r_1
        self.f, self.g = f, g

    def enforce(self, net, r, theta, phi):
        r"""Enforce the output of a neural network to satisfy the boundary condition.

        :param net: The neural network that approximates the ODE.
        :type net: `torch.nn.Module`
        :param r: The radii of points where the neural network output is evaluated.
        :type r: `torch.tensor`
        :param theta: The latitudes of points where the neural network output is evaluated. `theta` ranges [0, pi]
        :type theta: `torch.tensor`
        :param phi: The longitudes of points where the neural network output is evaluated. `phi` ranges [0, 2*pi)
        :type phi: `torch.tensor`
        :return: The modified output which now satisfies the boundary condition.
        :rtype: `torch.tensor`


        .. note::
            `enforce` is meant to be called by the function `solve_spherical` and `solve_spherical_system`.
        """
        u = _nn_output_spherical_input(net, r, theta, phi)
        if self.r_1 is None:
            return (1 - torch.exp(-r + self.r_0)) * u + self.f(theta, phi)
        else:
            r_tilde = (r - self.r_0) / (self.r_1 - self.r_0)
            # noinspection PyTypeChecker
            return self.f(theta, phi) * (1 - r_tilde) + \
                   self.g(theta, phi) * r_tilde + \
                   (1. - torch.exp((1 - r_tilde) * r_tilde)) * u


class InfDirichletBVPSpherical(BaseConditionSpherical):
    """Similar to `DirichletBVPSpherical`; only difference is we are considering :math:`g(\\theta, \\phi)` as :math:`r_1 \\to \\infty`, so `r_1` doesn't need to be specified
        We are solving :math:`u(t)` given :math:`u(r, \\theta, \\phi)\\bigg|_{r = r_0} = f(\\theta, \\phi)` and :math:`\\lim_{r \\to \\infty} u(r, \\theta, \\phi) = g(\\theta, \\phi)`

    :param r_0: The radius of the interior boundary. When r_0 = 0, the interior boundary is collapsed to a single point (center of the ball)
    :type r_0: float
    :param f: The value of :math:u on the interior boundary. :math:`u(r, \\theta, \\phi)\\bigg|_{r = r_0} = f(\\theta, \\phi)`.
    :type f: function
    :param g: The value of :math:u on the exterior boundary. :math:`u(r, \\theta, \\phi)\\bigg|_{r = r_1} = g(\\theta, \\phi)`.
    :type g: function
    :param order: The smallest :math:k that guarantees :math:`\\lim_{r \\to +\\infty} u(r, \\theta, \\phi) e^{-k r} = 0`, defaults to 1
    :type order: int or float, optional
    """

    def __init__(self, r_0, f, g, order=1):
        self.r_0 = r_0
        self.f = f
        self.g = g
        self.order = order

    def enforce(self, net, r, theta, phi):
        r"""Enforce the output of a neural network to satisfy the boundary condition.

        :param net: The neural network that approximates the PDE.
        :type net: `torch.nn.Module`
        :param r: The radii of points where the neural network output is evaluated.
        :type r: `torch.tensor`
        :param theta: The latitudes of points where the neural network output is evaluated. `theta` ranges [0, pi]
        :type theta: `torch.tensor`
        :param phi: The longitudes of points where the neural network output is evaluated. `phi` ranges [0, 2*pi)
        :type phi: `torch.tensor`
        :return: The modified output which now satisfies the boundary condition.
        :rtype: `torch.tensor`


        .. note::
            `enforce` is meant to be called by the function `solve_spherical` and `solve_spherical_system`.
        """
        u = _nn_output_spherical_input(net, r, theta, phi)
        dr = r - self.r_0
        return self.f(theta, phi) * torch.exp(-self.order * dr) + \
               self.g(theta, phi) * torch.tanh(dr) + \
               torch.exp(-self.order * dr) * torch.tanh(dr) * u


class SolutionSpherical:
    """A solution to a PDE (system) in spherical coordinates

    :param nets: The neural networks that approximate the PDE.
    :type nets: list[`torch.nn.Module`]
    :param conditions: The conditions of the PDE (system).
    :type conditions: list[`neurodiffeq.pde_spherical.BaseConditionSpherical`]
    """

    def __init__(self, nets, conditions):
        """Initializer method
        """
        self.nets = deepcopy(nets)
        self.conditions = deepcopy(conditions)

    def _compute_u(self, net, condition, rs, thetas, phis):
        return condition.enforce(net, rs, thetas, phis)

    def __call__(self, rs, thetas, phis, as_type='tf'):
        """Evaluate the solution at certain points.

        :param rs: The radii of points where the neural network output is evaluated.
        :type rs: `torch.tensor`
        :param thetas: The latitudes of points where the neural network output is evaluated. `theta` ranges [0, pi]
        :type thetas: `torch.tensor`
        :param phis: The longitudes of points where the neural network output is evaluated. `phi` ranges [0, 2*pi)
        :type phis: `torch.tensor`
        :param as_type: Whether the returned value is a `torch.tensor` ('tf') or `numpy.array` ('np').
        :type as_type: str
        :return: dependent variables are evaluated at given points.
        :rtype: list[`torch.tensor` or `numpy.array` (when there is more than one dependent variables)
            `torch.tensor` or `numpy.array` (when there is only one dependent variable)
        """
        if not isinstance(rs, torch.Tensor):
            rs = torch.tensor(rs, dtype=torch.float32)
        if not isinstance(thetas, torch.Tensor):
            thetas = torch.tensor(thetas, dtype=torch.float32)
        if not isinstance(phis, torch.Tensor):
            phis = torch.tensor(phis, dtype=torch.float32)
        original_shape = rs.shape
        rs = rs.reshape(-1, 1)
        thetas = thetas.reshape(-1, 1)
        phis = phis.reshape(-1, 1)
        if as_type not in ('tf', 'np'):
            raise ValueError("The valid return types are 'tf' and 'np'.")

        vs = [
            self._compute_u(net, con, rs, thetas, phis).reshape(original_shape)
            for con, net in zip(self.conditions, self.nets)
        ]
        if as_type == 'np':
            vs = [v.detach().cpu().numpy().flatten() for v in vs]

        return vs if len(self.nets) > 1 else vs[0]


def solve_spherical(
        pde, condition, r_min=None, r_max=None,
        net=None, train_generator=None, shuffle=True, valid_generator=None, analytic_solution=None,
        optimizer=None, criterion=None, batch_size=16, max_epochs=1000,
        monitor=None, return_internal=False, return_best=False
):
    """[DEPRECATED, use SphericalSolver class instead] Train a neural network to solve one PDE with spherical inputs in 3D space

        :param pde: The PDE to solve. If the PDE is :math:`F(u, r,\\theta, \\phi) = 0` where :math:`u` is the dependent variable and :math:`r`, :math:`\\theta` and :math:`\\phi` are the independent variables,
            then `pde` should be a function that maps :math:`(u, r, \\theta, \\phi)` to :math:`F(u, r,\\theta, \\phi)`
        :type pde: function
        :param condition: The initial/boundary condition that :math:`u` should satisfy.
        :type condition: `neurodiffeq.pde_spherical.BaseConditionSpherical`
        :param r_min: radius for inner boundary; ignored if both generators are provided; optional
        :type r_min: float
        :param r_max: radius for outer boundary; ignored if both generators are provided; optional
        :type r_max: float
        :param net: The neural network used to approximate the solution, defaults to None.
        :type net: `torch.nn.Module`, optional
        :param train_generator: The example generator to generate 3-D training points, default to None.
        :type train_generator: `neurodiffeq.pde_spherical.BaseGenerator`, optional
        :param valid_generator: The example generator to generate 3-D validation points, default to None.
        :type valid_generator: `neurodiffeq.pde_spherical.BaseGenerator`, optional
        :param shuffle: Whether to shuffle the training examples every epoch, defaults to True.
        :type shuffle: bool, optional
        :param analytic_solution: analytic solution to the pde system, used for testing purposes; should map (rs, thetas, phis) to u
        :type analytic_solution: function
        :param optimizer: The optimization method to use for training, defaults to None.
        :type optimizer: `torch.optim.Optimizer`, optional
        :param criterion: The loss function to use for training, defaults to None.
        :type criterion: `torch.nn.modules.loss._Loss`, optional
        :param batch_size: The shape of the mini-batch to use, defaults to 16.
        :type batch_size: int, optional
        :param max_epochs: The maximum number of epochs to train, defaults to 1000.
        :type max_epochs: int, optional
        :param monitor: The monitor to check the status of neural network during training, defaults to None.
        :type monitor: `neurodiffeq.pde_spherical.MonitorSpherical`, optional
        :param return_internal: Whether to return the nets, conditions, training generator, validation generator, optimizer and loss function, defaults to False.
        :type return_internal: bool, optional
        :param return_best: Whether to return the nets that achieved the lowest validation loss, defaults to False.
        :type return_best: bool, optional
        :return: The solution of the PDE. The history of training loss and validation loss.
            Optionally, MSE against analytic solution, the nets, conditions, training generator, validation generator, optimizer and loss function.
            The solution is a function that has the signature `solution(xs, ys, as_type)`.
        :rtype: tuple[`neurodiffeq.pde_spherical.SolutionSpherical`, dict]; or tuple[`neurodiffeq.pde_spherical.SolutionSpherical`, dict, dict]; or tuple[`neurodiffeq.pde_spherical.SolutionSpherical`, dict, dict, dict]
        """

    print("solve_spherical is deprecated, consider using SphericalSolver instead", file=sys.stderr)
    pde_sytem = lambda u, r, theta, phi: [pde(u, r, theta, phi)]
    conditions = [condition]
    nets = [net] if net is not None else None
    if analytic_solution is None:
        analytic_solutions = None
    else:
        analytic_solutions = lambda r, theta, phi: [analytic_solution(r, theta, phi)]

    return solve_spherical_system(
        pde_system=pde_sytem, conditions=conditions, r_min=r_min, r_max=r_max,
        nets=nets, train_generator=train_generator, shuffle=shuffle, valid_generator=valid_generator,
        analytic_solutions=analytic_solutions, optimizer=optimizer, criterion=criterion, batch_size=batch_size,
        max_epochs=max_epochs, monitor=monitor, return_internal=return_internal, return_best=return_best,
    )


def solve_spherical_system(
        pde_system, conditions, r_min=None, r_max=None,
        nets=None, train_generator=None, shuffle=True, valid_generator=None, analytic_solutions=None,
        optimizer=None, criterion=None, batch_size=16,
        max_epochs=1000, monitor=None, return_internal=False, return_best=False
):
    """[DEPRECATED, use SphericalSolver class instead] Train a neural network to solve a PDE system with spherical inputs in 3D space

        :param pde_system: The PDEs ystem to solve. If the PDE is :math:`F_i(u_1, u_2, ..., u_n, r,\\theta, \\phi) = 0` where :math:`u_i` is the i-th dependent variable and :math:`r`, :math:`\\theta` and :math:`\\phi` are the independent variables,
            then `pde_system` should be a function that maps :math:`(u_1, u_2, ..., u_n, r, \\theta, \\phi)` to a list where the i-th entry is :math:`F_i(u_1, u_2, ..., u_n, r, \\theta, \\phi)`.
        :type pde_system: function
        :param conditions: The initial/boundary conditions. The ith entry of the conditions is the condition that :math:`u_i` should satisfy.
        :type conditions: list[`neurodiffeq.pde_spherical.BaseConditionSpherical`]
        :param r_min: radius for inner boundary; ignored if both generators are provided; optional
        :type r_min: float
        :param r_max: radius for outer boundary; ignored if both generators are provided; optional
        :type r_max: float
        :param nets: The neural networks used to approximate the solution, defaults to None.
        :type nets: list[`torch.nn.Module`], optionalnerate 3-D training points, default to None.
        :param train_generator: The example generator to generate 3-D training points, default to None.
        :type train_generator: `neurodiffeq.pde_spherical.BaseGenerator`, optional
        :param valid_generator: The example generator to generate 3-D validation points, default to None.
        :type valid_generator: `neurodiffeq.pde_spherical.BaseGenerator`, optional
        :param shuffle: deprecated and ignored; shuffling should be implemented in genrators
        :type shuffle: bool, optional
        :param analytic_solutions: analytic solution to the pde system, used for testing purposes; should map (rs, thetas, phis) to a list of [u_1, u_2, ..., u_n]
        :type analytic_solutions: function
        :param optimizer: The optimization method to use for training, defaults to None.
        :type optimizer: `torch.optim.Optimizer`, optional
        :param criterion: The loss function to use for training, defaults to None.
        :type criterion: `torch.nn.modules.loss._Loss`, optional
        :param batch_size: The shape of the mini-batch to use, defaults to 16.
        :type batch_size: int, optional
        :param max_epochs: The maximum number of epochs to train, defaults to 1000.
        :type max_epochs: int, optional
        :param monitor: The monitor to check the status of neural network during training, defaults to None.
        :type monitor: `neurodiffeq.pde_spherical.MonitorSpherical`, optional
        :param return_internal: Whether to return the nets, conditions, training generator, validation generator, optimizer and loss function, defaults to False.
        :type return_internal: bool, optional
        :param return_best: Whether to return the nets that achieved the lowest validation loss, defaults to False.
        :type return_best: bool, optional
        :return: The solution of the PDE. The history of training loss and validation loss.
            Optionally, MSE against analytic solutions, the nets, conditions, training generator, validation generator, optimizer and loss function.
            The solution is a function that has the signature `solution(xs, ys, as_type)`.
        :rtype: tuple[`neurodiffeq.pde_spherical.SolutionSpherical`, dict]; or tuple[`neurodiffeq.pde_spherical.SolutionSpherical`, dict, dict]; or tuple[`neurodiffeq.pde_spherical.SolutionSpherical`, dict, dict, dict]
        """
    # default values
    print("solve_spherical_system is deprecated, consider using SphericalSolver instead", file=sys.stderr)

    solver = SphericalSolver(
        pde_system=pde_system,
        conditions=conditions,
        r_min=r_min,
        r_max=r_max,
        nets=nets,
        train_generator=train_generator,
        valid_generator=valid_generator,
        analytic_solutions=analytic_solutions,
        optimizer=optimizer,
        criterion=criterion,
        batch_size=batch_size,
        shuffle=shuffle,
    )

    solver.fit(max_epochs=max_epochs, monitor=monitor)
    solution = solver.get_solution(copy=True, best=return_best)
    ret = (solution, solver.loss)
    if analytic_solutions is not None:
        ret = ret + (solver.analytic_mse,)
    if return_internal:
        params = ['nets', 'conditions', 'train_generator', 'valid_generator', 'optimizer', 'criterion']
        internals = solver.get_internals(params, return_type="dict")
        ret = ret + (internals,)
    return ret


class SphericalSolver:
    """A solver class for solving PDEs in spherical coordinates
    
    :param pde_system: the PDE system to solve; maps a tuple of three coordinates to a tuple of PDE residuals, both the coordinates and PDE residuals must have shape (-1, 1)
    :type pde_system: callable
    :param conditions: list of boundary conditions for each target function
    :type conditions: list[`neurodiffeq.pde_spherical.BaseConditionSpherical`]
    :param r_min: radius for inner boundary; ignored if train_generator and valid_generator are both set; r_min > 0; optional
    :type r_min: float
    :param r_max: radius for outer boundary; ignored if train_generator and valid_generator are both set; r_max > r_min; optional
    :type r_max: float
    :param nets: list of neural networks for parameterized solution; if provided, length must equal that of conditions; optional
    :type nets: list[torch.nn.Module]
    :param train_generator: generator for sampling training points, must provide a .get_examples() method and a .size field; optional
    :type train_generator: `neurodiffeq.pde_spherical.BaseGenerator`
    :param valid_generator: generator for sampling validation points, must provide a .get_examples() method and a .size field; optional
    :type valid_generator: `neurodiffeq.pde_spherical.BaseGenerator`
    :param analytic_solutions: analytical solutions to be compared with neural net solutions; maps a tuple of three coordinates to a tuple of function values; output shape shoule match that of networks; optional
    :type analytic_solutions: callable
    :param optimizer: optimizer to be used for training; optional
    :type optimizer: torch.nn.optim.Optimizer
    :param criterion: function that maps a PDE residual vector (torch tensor with shape (-1, 1)) to a scalar loss; optional
    :type criterion: callable
    :param batch_size: batch size to be used for training and validation; optional
    :type batch_size: int
    :param shuffle: deprecated; shuffling should be performed by generators
    :type shuffle: bool
    """

    def __init__(self, pde_system, conditions, r_min=None, r_max=None,
                 nets=None, train_generator=None, valid_generator=None, analytic_solutions=None,
                 optimizer=None, criterion=None, batch_size=16, shuffle=False):

        if shuffle:
            print("param `shuffle` is deprecated and ignored; shuffling should be performed by generators",
                  file=sys.stderr)

        if train_generator is None or valid_generator is None:
            if r_min is None or r_max is None:
                raise ValueError(f"Either generator is not provided, r_min and r_max should be both provided: "
                                 f"got r_min={r_min}, r_max={r_max}, train_generator={train_generator}, "
                                 f"valid_generator={valid_generator}")

        self.pdes = pde_system
        self.conditions = conditions
        self.n_funcs = len(conditions)
        self.r_min = r_min
        self.r_max = r_max
        if nets is None:
            self.nets = [
                FCNN(n_input_units=3, n_hidden_units=32, n_hidden_layers=1, actv=nn.Tanh)
                for _ in range(self.n_funcs)
            ]
        else:
            self.nets = nets

        if train_generator is None:
            train_generator = ExampleGeneratorSpherical(512, r_min, r_max, method='equally-spaced-noisy')

        if valid_generator is None:
            valid_generator = ExampleGeneratorSpherical(512, r_min, r_max, method='equally-spaced')

        self.analytic_solutions = analytic_solutions

        if optimizer is None:
            all_params = []
            for n in self.nets:
                all_params += n.parameters()
            self.optimizer = optim.Adam(all_params, lr=0.001)
        else:
            self.optimizer = optimizer

        if criterion is None:
            self.criterion = lambda residual_tensor: (residual_tensor ** 2).mean()
        else:
            self.criterion = criterion

        self.batch_size = batch_size

        def make_pair_dict(train=None, valid=None):
            return {'train': train, 'valid': valid}

        self.generator = make_pair_dict(train=train_generator, valid=valid_generator)
        # loss history
        self.loss = make_pair_dict(train=[], valid=[])
        # analytic MSE history
        self.analytic_mse = make_pair_dict(train=[], valid=[])
        # starting index of the next batch, should only be modified through self._update_batch_start()
        self._batch_start = make_pair_dict(train=0, valid=0)
        # samples generated for the current epoch, should be resampled every epoch with self._resample()
        self._examples = make_pair_dict()
        # current batch of samples, kept for additional loss terms in future
        self._batch_examples = make_pair_dict()
        # current network with lowest loss
        self.best_nets = None
        # current lowest loss
        self.lowest_loss = None

    @staticmethod
    def _auto_enforce(net, cond, r, theta, phi):
        """automatically enforce condition on network with dynamic number of inputs
        if `cond.enforce()` takes two arguments, pass `net` and `r`
        if `cond.enforce()` takes four arguments, pass `net`, `r`, `theta`, and `phi`
        otherwise, raise a ValueError

        :param net: network for parameterized solution
        :type net: torch.nn.Module
        :param cond: condition (a.k.a. parameterization) for the network
        :type cond: `neurodiffeq.pde_spherical.BaseConditionSpherical`
        :param r: a vector of :math:`r`, shape = (-1, 1)
        :type r: torch.Tensor
        :param theta: a vector of :math:`\\theta`, shape = (-1, 1)
        :type theta: torch.Tensor
        :param phi: a vector of :math:`\\phi`, shape = (-1, 1)
        :type phi: torch.Tensor
        :return: function values at sampled points
        :rtype: torch.Tensor
        """
        n_params = len(signature(cond.enforce).parameters)
        if n_params == 2:
            # noinspection PyArgumentList
            return cond.enforce(net, r)
        elif n_params == 4:
            return cond.enforce(net, r, theta, phi)
        else:
            raise ValueError(f'unrecognized `condition.enforce` signature {signature(cond.enforce)}')

    def _update_history(self, value, metric_type, key):
        """append a value to corresponding history list

        :param value: value to be appended
        :type value: float
        :param metric_type: {'loss', 'analytic_mse'}; type of history metrics
        :type metric_type: str
        :param key: {'train', 'valid'}; dict key in self.loss / self.analytic_mse
        :type key: str
        """
        if metric_type == 'loss':
            self.loss[key].append(value)
        elif metric_type == 'analytic_mse':
            self.analytic_mse[key].append(value)
        else:
            raise KeyError(f'history type = {metric_type} not understood')

    def _update_train_history(self, value, metric_type):
        """append a value to corresponding training history list"""
        self._update_history(value, metric_type, key='train')

    def _update_valid_history(self, value, metric_type):
        """append a value to corresponding validation history list"""
        self._update_history(value, metric_type, key='valid')

    def _reset_batch_start(self, key):
        """reset starting index of current batch to 0

        :param key: {'train', 'valid'}; dict key in self._batch_start
        :type key: str
        """
        self._batch_start[key] = 0

    def _reset_train_batch_start(self):
        """reset starting index of current training batch to 0"""
        self._reset_batch_start(key='train')

    def _reset_valid_batch_start(self):
        """reset starting index of current validation batch to 0"""
        self._reset_batch_start(key='valid')

    def _resample(self, key):
        """resample points for the current epoch, and register in self._examples

        :param key: {'train', 'valid'}; dict key in self._examples / self.generator
        :type key: str
        """
        self._examples[key] = [var.reshape(-1, 1) for var in self.generator[key].get_examples()]

    def _resample_train(self):
        """resample training points for the current epoch, and register in self._examples"""
        self._resample('train')

    def _resample_valid(self):
        """resample validation points for the current epoch, and register in self_examples"""
        self._resample('valid')

    def _generate_batch(self, key):
        """generate the next batch, register in self._batch_examples and return the batch

        :param key: {'train', 'valid'}; dict key in self._examples / self._batch_examples / self._batch_start
        :type key: str
        """
        old_start = self._batch_start[key]
        new_start = min(old_start + self.batch_size, self.generator[key].size)
        self._batch_start[key] = new_start
        # the following side effects are helpful for future extension,
        # especially for additional loss term that depends on the coordinates
        self._batch_examples[key] = [var[old_start: new_start] for var in self._examples[key]]
        return self._batch_examples[key]

    def _generate_train_batch(self):
        """generate the next training batch, register in self._batch_examples and return"""
        return self._generate_batch('train')

    def _generate_valid_batch(self):
        """generate the next validation batch, register in self._batch_examples and return"""
        return self._generate_batch('valid')

    def _run_epoch(self, key):
        """run an epoch on train/valid points, update history, and perform gradient descent if key=='train'
        this method doesn't resample points, which shall be handled in the `.fit()` call

        :param key: {'train', 'valid'}; phase of the epoch
        :type key: str
        """
        # perform forward pass for all batches
        epoch_loss = 0
        epoch_analytic_mse = 0
        while self._batch_start[key] < self.generator[key].size:
            r, theta, phi = self._generate_batch(key)
            # n_samples is not necessarily self.batch_size when batch size doesn't divide generator size
            n_samples = len(r)
            funcs = [
                self._auto_enforce(n, c, r, theta, phi) for n, c in zip(self.nets, self.conditions)
            ]

            if self.analytic_solutions is not None:
                funcs_true = self.analytic_solutions(r, theta, phi)
                for f_pred, f_true in zip(funcs, funcs_true):
                    epoch_analytic_mse += ((f_pred - f_true) ** 2).sum().item()

            residuals = self.pdes(*funcs, r, theta, phi)
            residuals = torch.stack(residuals)
            loss = self.criterion(residuals)
            epoch_loss += loss.item() * n_samples

            # perform optimization step when training
            if key == 'train':
                self.optimizer.zero_grad()
                loss.backward()
                self.optimizer.step()

        # calculate mean loss of all batches and register to history
        epoch_loss /= self.generator[key].size
        self._update_history(epoch_loss, 'loss', key)

        # calculate mean analytic mse of all batches and register to history
        if self.analytic_solutions is not None:
            epoch_analytic_mse /= self.generator[key].size
            epoch_analytic_mse /= self.n_funcs
            self._update_history(epoch_analytic_mse, 'analytic_mse', key)

    def run_train_epoch(self):
        """run a training epoch, update history, and perform gradient descent"""
        self._run_epoch('train')

    def run_valid_epoch(self):
        """run a validation epoch and update history"""
        self._run_epoch('valid')

    def _update_best(self):
        """update self.lowest_loss and self.best_nets if current validation loss is lower than self.lowest_loss"""
        current_loss = self.loss['valid'][-1]
        if (self.lowest_loss is None) or current_loss < self.lowest_loss:
            self.lowest_loss = current_loss
            self.best_nets = deepcopy(self.nets)

    def fit(self, max_epochs, monitor=None):
        """run multiple epochs of training and validation, update best loss at the end of each epoch;
        this method does not return solution, which is done in the `.get_solution` method
        :param max_epochs: number of epochs to run
        :type max_epochs: int
        :param monitor: monitor for visualizing solution and metrics
        :rtype monitor: `neurodiffeq.pde_spherical.MonitorSpherical`
        """
        for epoch in range(max_epochs):
            self._resample_train()
            self._resample_valid()
            self._reset_train_batch_start()
            self._reset_valid_batch_start()
            self.run_train_epoch()
            self.run_valid_epoch()
            self._update_best()

            if monitor:
                if (epoch + 1) % monitor.check_every == 0 or epoch == max_epochs - 1:
                    monitor.check(
                        self.nets,
                        self.conditions,
                        loss_history=self.loss,
                        analytic_mse_history=self.analytic_solutions
                    )

    def get_solution(self, copy=True, best=True):
        """return a solution class
        :param copy: if True, use a deep copy of internal nets and conditions
        :type copy: bool
        :param best: if True, return the solution with lowest loss instead of the solution after the last epoch
        :type best: bool
        :return: trained solution
        :rtype: `neurodiffeq.pde_spherical.SolutionSpherical`
        """
        nets = self.best_nets if best else self.nets
        conditions = self.conditions
        if copy:
            nets = deepcopy(nets)
            conditions = deepcopy(conditions)

        return SolutionSpherical(nets, conditions)

    def get_internals(self, param_names, return_type='list'):
        """return internal variable(s) of the solver
        if param_names == 'all', return all internal variables as a dict;
        if param_names is single str, return the corresponding variables
        if param_names is a list and return_type == 'list', return corresponding internal variables as a list
        if param_names is a list and return_type == 'dict', return a dict with keys in param_names

        :param param_names: a parameter name or a list of parameter names
        :type param_names: str or list[str]
        :param return_type: {'list', 'dict'}; ignored if `param_names` is a str
        :type return_type: str
        :return: a single parameter, or a list/dict of parameters as indicated above
        :rtype: list or dict or any
        """
        # get internals according to provided param_names
        available_params = {
            "analytic_mse": self.analytic_mse,
            "analytic_solutions": self.analytic_solutions,
            "batch_size": self.batch_size,
            "best_nets": self.best_nets,
            "criterion": self.criterion,
            "loss": self.loss,
            "lowest_loss": self.lowest_loss,
            "n_funcs": self.n_funcs,
            "nets": self.best_nets,
            "optimizer": self.optimizer,
            "pdes": self.pdes,
            "r_max": self.r_max,
            "r_min": self.r_min,
        }

        if param_names == "all":
            return available_params

        if isinstance(param_names, str):
            return available_params[param_names]

        if return_type == 'list':
            return [available_params[name] for name in param_names]
        elif return_type == "dict":
            return {name: available_params[name] for name in param_names}
        else:
            raise ValueError(f"unrecognized return_type = {return_type}")


class MonitorSpherical:
    """A monitor for checking the status of the neural network during training.

    :param r_min: The lower bound of radius, i.e., radius of interior boundary
    :type r_min: float
    :param r_max: The upper bound of radius, i.e., radius of exterior boundary
    :type r_max: float
    :param check_every: The frequency of checking the neural network represented by the number of epochs between two checks, defaults to 100.
    :type check_every: int, optional
    :param var_names: names of dependent variables; if provided, shall be used for plot titles; defaults to None
    :type var_names: list[str]
    :param shape: shape of mesh for visualizing the solution; defaults to (10, 10, 10)
    :type shape: tuple[int]
    :param r_scale: 'linear' or 'log'; controls the grid point in the :math:`r` direction; defaults to 'linear'
    :type r_scale: str
    """

    def __init__(self, r_min, r_max, check_every=100, var_names=None, shape=(10, 10, 10), r_scale='linear'):
        """Initializer method
        """
        self.contour_plot_available = self._matplotlib_version_satisfies()
        if not self.contour_plot_available:
            print("Warning: contourf plot only available for matplotlib version >= v3.3.0"
                  "switching to matshow instead", file=sys.stderr)
        self.using_non_gui_backend = (matplotlib.get_backend() == 'agg')
        self.check_every = check_every
        self.fig = None
        self.axs = []  # subplots
        self.cbs = []  # color bars
        self.names = var_names
        self.shape = shape
        # input for neural network

        if r_scale == 'log':
            r_min, r_max = np.log(r_min), np.log(r_max)

        gen = ExampleGenerator3D(
            grid=shape,
            xyz_min=(r_min, 0., 0.),
            xyz_max=(r_max, np.pi, 2 * np.pi),
            method='equally-spaced'
        )
        rs, thetas, phis = gen.get_examples()  # type: torch.Tensor, torch.Tensor, torch.Tensor

        if r_scale == 'log':
            rs = torch.exp(rs)

        self.r_tensor = rs.reshape(-1, 1)
        self.theta_tensor = thetas.reshape(-1, 1)
        self.phi_tensor = phis.reshape(-1, 1)

        self.r_label = rs.reshape(-1).detach().cpu().numpy()
        self.theta_label = thetas.reshape(-1).detach().cpu().numpy()
        self.phi_label = phis.reshape(-1).detach().cpu().numpy()

    @staticmethod
    def _matplotlib_version_satisfies():
        from packaging.version import parse as vparse
        from matplotlib import __version__
        return vparse(__version__) >= vparse('3.3.0')

    @staticmethod
    def _longitude_formatter(value, count):
        value = int(round(value / np.pi * 180)) - 180
        if value == 0 or abs(value) == 180:
            marker = ''
        elif value > 0:
            marker = 'E'
        else:
            marker = 'W'
        return f'{abs(value)}°{marker}'

    @staticmethod
    def _latitude_formatter(value, count):
        value = int(round(value / np.pi * 180)) - 90
        if value == 0:
            marker = ''
        elif value > 0:
            marker = 'N'
        else:
            marker = 'S'
        return f'{abs(value)}°{marker}'

    def _compute_us(self, nets, conditions):
        r, theta, phi = self.r_tensor, self.theta_tensor, self.phi_tensor
        return [
            cond.enforce(net, r, theta, phi).detach().cpu().numpy()
            for cond, net in zip(nets, conditions)
        ]

    def check(self, nets, conditions, loss_history, analytic_mse_history=None):
        r"""Draw (3n + 2) plots:
             1) For each function u(r, phi, theta), there are 3 axes:
                a) one ax for u-r curves grouped by phi
                b) one ax for u-r curves grouped by theta
                c) one ax for u-theta-phi contour heat map
             2) Additionally, one ax for MSE against analytic solution, another for training and validation loss

        :param nets: The neural networks that approximates the PDE.
        :type nets: list [`torch.nn.Module`]
        :param conditions: The initial/boundary condition of the PDE.
        :type conditions: list [`neurodiffeq.pde_spherical.BaseConditionSpherical`]
        :param loss_history: The history of training loss and validation loss. The 'train' entry is a list of training loss and 'valid' entry is a list of validation loss.
        :type loss_history: dict['train': list[float], 'valid': list[float]]
        :param analytic_mse_history: The history of training and validation MSE against analytic solution. The 'train' entry is a list of training analytic MSE and 'valid' entry is a list of validation analytic MSE.
        :type analytic_mse_history: dict['train': list[float], 'valid': list[float]]

        .. note::
            `check` is meant to be called by the function `solve2D`.
        """

        # initialize the figure and axes here so that the Monitor knows the number of dependent variables and
        # shape of the figure, number of the subplots, etc.
        # Draw (3n + 2) plots:
        #     1) For each function u(r, phi, theta), there are 3 axes:
        #         a) one ax for u-r curves grouped by phi
        #         b) one ax for u-r curves grouped by theta
        #         c) one ax for u-theta-phi contour heat map
        #     2) Additionally, one ax for MSE against analytic solution, another for training and validation loss
        n_row = len(nets) + 1
        n_col = 3
        if not self.fig:
            self.fig = plt.figure(figsize=(24, 6 * n_row))
            self.fig.tight_layout()
            self.axs = self.fig.subplots(nrows=n_row, ncols=n_col, gridspec_kw={'width_ratios': [1, 1, 2]})
            self.cbs = [None] * len(nets)

        us = self._compute_us(nets, conditions)

        for i, u in enumerate(us):
            try:
                var_name = self.names[i]
            except (TypeError, IndexError):
                var_name = f"u[{i}]"

            # prepare data for plotting
            u_across_r = u.reshape(*self.shape).mean(0)
            df = pd.DataFrame({
                '$r$': self.r_label,
                '$\\theta$': self.theta_label,
                '$\\phi$': self.phi_label,
                'u': u.reshape(-1),
            })

            # ax for u-r curve grouped by phi
            ax = self.axs[i][0]
            ax.clear()
            sns.lineplot(x='$r$', y='u', hue='$\\phi$', data=df, ax=ax)
            ax.set_title(f'{var_name}($r$) grouped by $\\phi$')
            ax.set_ylabel(var_name)

            # ax for u-r curve grouped by theta
            ax = self.axs[i][1]
            ax.clear()
            sns.lineplot(x='$r$', y='u', hue='$\\theta$', data=df, ax=ax)
            ax.set_title(f'{var_name}($r$) grouped by $\\theta$')
            ax.set_ylabel(var_name)

            # u-theta-phi heatmap/contourf depending on matplotlib version
            ax = self.axs[i][2]
            ax.clear()
            ax.set_xlabel('$\\phi$')
            ax.set_ylabel('$\\theta$')

            ax.set_title(f'{var_name} averaged across $r$')
            if self.contour_plot_available:
                # matplotlib has problems plotting repeatedly `contourf` until version 3.3
                # see https://github.com/matplotlib/matplotlib/issues/15986
                theta = self.theta_label.reshape(*self.shape)[0, :, 0]
                phi = self.phi_label.reshape(*self.shape)[0, 0, :]
                cax = ax.contourf(phi, theta, u_across_r, cmap='magma')
                ax.xaxis.set_major_locator(plt.MultipleLocator(np.pi / 6))
                ax.xaxis.set_minor_locator(plt.MultipleLocator(np.pi / 12))
                ax.xaxis.set_major_formatter(plt.FuncFormatter(self._longitude_formatter))
                ax.yaxis.set_major_locator(plt.MultipleLocator(np.pi / 6))
                ax.yaxis.set_minor_locator(plt.MultipleLocator(np.pi / 12))
                ax.yaxis.set_major_formatter(plt.FuncFormatter(self._latitude_formatter))
                ax.grid(which='major', linestyle='--', linewidth=0.5)
                ax.grid(which='minor', linestyle=':', linewidth=0.5)
            else:
                # use matshow() to plot a heatmap instead
                cax = ax.matshow(u_across_r, cmap='magma', interpolation='nearest')

            if self.cbs[i]:
                self.cbs[i].remove()
            self.cbs[i] = self.fig.colorbar(cax, ax=ax)

        ax = self.axs[n_row - 1][0]
        ax.clear()
        ax.set_title('MSE against analytic solution')
        ax.set_ylabel('MSE')
        ax.set_xlabel('epochs')
        if analytic_mse_history:
            ax[-2].plot(analytic_mse_history['train'], label='training')
            ax[-2].plot(analytic_mse_history['valid'], label='validation')
            ax[-2].set_yscale('log')
            ax[-2].legend()

        ax = self.axs[n_row - 1][1]
        ax.clear()
        ax.plot(loss_history['train'], label='training loss')
        ax.plot(loss_history['valid'], label='validation loss')
        ax.set_title('loss during training')
        ax.set_ylabel('loss')
        ax.set_xlabel('epochs')
        ax.set_yscale('log')
        ax.legend()

        self.fig.canvas.draw()
        # for command-line, interactive plots, not pausing can lead to graphs not being displayed at all
        # see https://stackoverflow.com/questions/19105388/python-2-7-mac-osx-interactive-plotting-with-matplotlib-not-working
        if not self.using_non_gui_backend:
            plt.pause(0.05)

    def new(self):
        self.fig = None
        self.axs = []
        self.cbs = []
        return self


class BaseConditionSphericalHarmonics(BaseConditionSpherical):
    """
    :param max_degree: highest degree for spherical harmonics
    :type max_degree: int
    """

    def __init__(self, max_degree=4):
        self.max_degree = max_degree

    # noinspection PyMethodOverriding
    def enforce(self, net, r):
        raise NotImplementedError(f'Abstract BVP {self.__class__.__name__} cannot be enforced')


class NoConditionSphericalHarmonics(BaseConditionSphericalHarmonics):
    def enforce(self, net, r):
        return net(r)


class DirichletBVPSphericalHarmonics(BaseConditionSphericalHarmonics):
    """Similar to `DirichletBVPSpherical`; only difference is this condition is enforced on a neural net that takes in :math:r and returns the spherical harmonic coefficients R(r)
        i.e., we constrain the coefficients :math:`R(r)` of spherical harmonics instead of the inner product :math:`R(r) \\cdot Y(\\theta, \\phi)`
        We are solving :math:`R(r)` given :math:`R(r)\\bigg|_{r = r_0} = R_0` and :math:`R(r)\\bigg|_{r = r_1} = R_1`.

    :param r_0: The radius of the interior boundary. When r_0 = 0, the interior boundary is collapsed to a single point (center of the ball)
    :type r_0: float
    :param R_0: The value of harmonic coefficients :math:R on the interior boundary. :math:`R(r)\\bigg|_{r = r_0} = R_0`.
    :type R_0: torch.tensor
    :param r_1: The radius of the exterior boundary; if set to None, `R_1` must also be None
    :type r_1: float or None
    :param R_1: The value of harmonic coefficients :math:R on the exterior bounadry. :math:`R(r)\\bigg|_{r = r_1} = R_1`.
    :type R_1: torch.tensor
    :param max_degree: highest degree for spherical harmonics
    :type max_degree: int
    """

    def __init__(self, r_0, R_0, r_1=None, R_1=None, max_degree=4):
        """Initializer method
        """
        super(DirichletBVPSphericalHarmonics, self).__init__(max_degree=max_degree)
        if (r_1 is None) ^ (R_1 is None):
            raise ValueError(f'r_1 and R_1 must be both/neither set to None; got r_1={r_1}, R_1={R_1}')
        self.r_0, self.r_1 = r_0, r_1
        self.R_0, self.R_1 = R_0, R_1

    def enforce(self, net, r):
        r"""Enforce the output of a neural network to satisfy the boundary condition.

        :param net: The neural network that approximates the coefficients for spherical harmonics.
        :type net: `torch.nn.Module`
        :param r: The radii of points where the neural network output is evaluated.
        :type r: `torch.tensor`
        :return: The modified output which now satisfies the boundary condition.
        :rtype: `torch.tensor`


        .. note::
            `enforce` is meant to be called by the function `solve_spherical` and `solve_spherical_system`.
        """
        R_raw = net(r)
        if self.r_1 is None:
            # noinspection PyTypeChecker
            ret = (1 - torch.exp(-r + self.r_0)) * R_raw + self.R_0
        else:
            r_tilde = (r - self.r_0) / (self.r_1 - self.r_0)
            # noinspection PyTypeChecker
            ret = self.R_0 * (1 - r_tilde) + self.R_1 * r_tilde + (1. - torch.exp((1 - r_tilde) * r_tilde)) * R_raw
        return ret


class InfDirichletBVPSphericalHarmonics(BaseConditionSphericalHarmonics):
    """Similar to `InfDirichletBVPSpherical`; only difference is this condition is enforced on a neural net that takes in :math:r and returns the spherical harmonic coefficients R(r)
        i.e., we constrain the coefficients :math:`R(r)` of spherical harmonics instead of the inner product :math:`R(r) \\cdot Y(\\theta, \\phi)`
        We are solving :math:`R(r)` given :math:`R(r)\\bigg|_{r = r_0} = R_0` and :math:`\\lim_{r \\to \\infty} R(r) = R_\\infty`

    :param r_0: The radius of the interior boundary. When r_0 = 0, the interior boundary is collapsed to a single point (center of the ball)
    :type r_0: float
    :param R_0: The value of harmonic coefficients :math:R on the interior boundary. :math:`R(r)\\bigg|_{r = r_0} = R_0`.
    :type R_0: torch.tensor
    :param R_inf: The value of harmonic coefficients :math:R at infinity. :math:`\\lim_{r \\to \\infty} R(r) = R_\\infty`.
    :type R_inf: torch.tensor
    :param order: The smallest :math:k that guarantees :math:`\\lim_{r \\to +\\infty} R(r) e^{-k r} = \\bf 0`, defaults to 1
    :type order: int or float, optional
    :param max_degree: highest degree for spherical harmonics
    :type max_degree: int
    """

    def __init__(self, r_0, R_0, R_inf, order=1, max_degree=4):
        super(InfDirichletBVPSphericalHarmonics, self).__init__(max_degree=max_degree)
        self.r_0 = r_0
        self.R_0 = R_0
        self.R_inf = R_inf
        self.order = order

    def enforce(self, net, r):
        r"""Enforce the output of a neural network to satisfy the boundary condition.

        :param net: The neural network that approximates the coefficients for the spherical harmonics.
        :type net: `torch.nn.Module`
        :param r: The radii of points where the neural network output is evaluated.
        :type r: `torch.tensor`
        :return: The modified output which now satisfies the boundary condition.
        :rtype: `torch.tensor`

        .. note::
            `enforce` is meant to be called by the function `solve_spherical` and `solve_spherical_system`.
        """
        R_raw = net(r)
        dr = r - self.r_0
        return self.R_0 * torch.exp(-self.order * dr) + \
               self.R_inf * torch.tanh(dr) + \
               torch.exp(-self.order * dr) * torch.tanh(dr) * R_raw


class SolutionSphericalHarmonics(SolutionSpherical):
    """
    A solution to a PDE (system) in spherical coordinates

    :param nets: list of networks that takes in radius tensor and outputs the coefficients of spherical harmonics
    :type nets: list[`torch.nn.Module`]
    :param conditions: list of conditions to be enforced on each nets; must be of the same length as nets
    :type conditions: list[BaseConditionSphericalHarmonics]
    :param max_degree: max_degree for spherical harmonics; defaults to 4
    :type max_degree: int
    """

    def __init__(self, nets, conditions, max_degree=4):
        super(SolutionSphericalHarmonics, self).__init__(nets, conditions)
        self.max_degree = max_degree
        self.harmonics_fn = RealSphericalHarmonics(max_degree=max_degree)

    def _compute_u(self, net, condition, rs, thetas, phis):
        products = condition.enforce(net, rs) * self.harmonics_fn(thetas, phis)
        return torch.sum(products, dim=1)


class SolutionCylindricalFourier(SolutionSpherical):
    def __init__(self, nets, conditions, max_degree=4):
        from .cylindrical_fourier_series import RealFourierSeries
        super(SolutionCylindricalFourier, self).__init__(nets, conditions)
        self.max_degree = max_degree
        self.harmonics_fn = RealFourierSeries(max_degree=max_degree)

    def _compute_u(self, net, condition, rs, thetas, phis):
        products = condition.enforce(net, rs) * self.harmonics_fn(thetas, phis)
        return torch.sum(products, dim=1)


class MonitorSphericalHarmonics(MonitorSpherical):
    """A monitor for checking the status of the neural network during training.

    :param r_min: The lower bound of radius, i.e., radius of interior boundary
    :type r_min: float
    :param r_max: The upper bound of radius, i.e., radius of exterior boundary
    :type r_max: float
    :param check_every: The frequency of checking the neural network represented by the number of epochs between two checks, defaults to 100.
    :type check_every: int, optional
    :param var_names: names of dependent variables; if provided, shall be used for plot titles; defaults to None
    :type var_names: list[str]
    :param max_degree: highest degree for spherical harmonics; defaults to None
    :type var_names: list[str]
    :param shape: shape of mesh for visualizing the solution; defaults to (10, 10, 10)
    :type shape: tuple[int]
    """

    def __init__(self, r_min, r_max, check_every=100, var_names=None, shape=(10, 10, 10), r_scale='linear',
                 max_degree=4):
        super(MonitorSphericalHarmonics, self).__init__(
            r_min,
            r_max,
            check_every=check_every,
            var_names=var_names,
            shape=shape,
            r_scale=r_scale,
        )

        self.max_degree = max_degree
        self.harmonics_fn = RealSphericalHarmonics(max_degree=max_degree)

    def _compute_us(self, nets, conditions):
        r, theta, phi = self.r_tensor, self.theta_tensor, self.phi_tensor
        us = []
        for net, cond in zip(nets, conditions):
            products = cond.enforce(net, r) * self.harmonics_fn(theta, phi)
            u = torch.sum(products, dim=1, keepdim=True).detach().cpu().numpy()
            us.append(u)
        return us
