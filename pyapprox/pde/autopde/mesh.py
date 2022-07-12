import torch
from functools import partial
import matplotlib.tri as tri
import numpy as np

from pyapprox.util.utilities import cartesian_product, outer_product
from pyapprox.surrogates.orthopoly.quadrature import gauss_jacobi_pts_wts_1D
from pyapprox.variables.transforms import _map_hypercube_samples
from pyapprox.surrogates.interp.barycentric_interpolation import (
    compute_barycentric_weights_1d, barycentric_interpolation_1d,
    multivariate_barycentric_lagrange_interpolation
)
from pyapprox.pde.spectralcollocation.spectral_collocation import (
    chebyshev_derivative_matrix, lagrange_polynomial_derivative_matrix_2d,
    lagrange_polynomial_derivative_matrix_1d, fourier_derivative_matrix,
    fourier_basis
)
from pyapprox.util.visualization import (
    get_meshgrid_function_data, plt, get_meshgrid_samples
)


class Canonical1DMeshBoundary():
    def __init__(self, bndry_name, tol=1e-15):
        self._bndry_index = {"left": 0, "right": 1}[bndry_name]
        self._normal = torch.tensor([[-1], [1]])[self._bndry_index]
        self._inactive_coord = {"left": -1, "right": 1}[bndry_name]
        self._tol = tol

    def normals(self, samples):
        return torch.tile(self._normal, (1, samples.shape[1])).T

    def quadrature_rule(self):
        return np.ones((1, 1)), np.ones((1, 1))

    def samples_on_boundary(self, samples):
        return np.where(
            np.absolute(self._inactive_coord-samples[0, :]) < self._tol)[0]


class Canonical2DMeshBoundary():
    def __init__(self, bndry_name, order, tol=1e-15):
        active_bounds = [-1, 1]
        if len(active_bounds) != 2:
            msg = "Bounds must be specfied for the dimension with the "
            msg += "varying coordinates"
            raise ValueError(msg)

        self._bndry_index = {
            "left": 0, "right": 1, "bottom": 2, "top": 3}[bndry_name]
        self._normal = torch.tensor(
            [[-1, 0], [1, 0], [0, -1], [0, 1]])[self._bndry_index]
        self._order = order
        self._active_bounds = active_bounds
        self._inactive_coord = {
            "left": -1, "right": 1, "bottom": -1, "top": 1}[bndry_name]
        self._tol = tol

    def normals(self, samples):
        return torch.tile(self._normal[:, None], (1, samples.shape[1])).T

    def quadrature_rule(self):
        nsamples = self._order+3
        xquad_1d, wquad_1d = gauss_jacobi_pts_wts_1D(nsamples, 0, 0)
        xlist = [None, None]
        xlist[int(self._bndry_index < 2)] = xquad_1d
        xlist[int(self._bndry_index >= 2)] = self._inactive_coord
        xquad = cartesian_product(xlist)
        wquad = wquad_1d[:, None]*np.prod(
            self._active_bounds[1::2]-self._active_bounds[::2])
        return xquad, wquad

    def samples_on_boundary(self, samples):
        dd = int(self._bndry_index >= 2)
        indices = np.where(
            np.absolute(self._inactive_coord-samples[dd, :]) < self._tol)[0]
        return indices


class Transformed2DMeshBoundary(Canonical2DMeshBoundary):
    # def __init__(self, bndry_name, order, bndry_deriv_vals, tol=1e-15):
    def __init__(self, bndry_name, order, normal_fun, tol=1e-15):
        super().__init__(bndry_name, order, tol)
        # self._bndary_deriv_vals = bndry_deriv_vals
        self._normal_fun = normal_fun
        self._active_var = int(self._bndry_index < 2)
        self._inactive_var = int(self._bndry_index >= 2)
        self._pts = -np.cos(np.linspace(0., np.pi, order+1))[None, :]
        self._bary_weights = compute_barycentric_weights_1d(self._pts[0, :])

    def normals(self, samples):
        if self._normal_fun is None:
            return super().normals(samples)
        normal_vals = self._normal_fun(samples)
        return normal_vals

    def _normals_from_derivs(self, samples):
        # compute normals numerically using mesh transforms
        # this will not give exact normals so we know pass in normal
        # function instead. This function is left incase it is needed in the future
        surface_derivs = self._interpolate(
            self._bndary_deriv_vals, samples[self._active_var])
        normals = torch.empty((samples.shape[1], 2))
        normals[:, self._active_var] = -torch.tensor(surface_derivs)
        normals[:, self._inactive_var] = 1
        factor = torch.sqrt(torch.sum(normals**2, axis=1))
        normals = 1/factor[:, None]*normals
        normals *= (-1)**((self._bndry_index+1) % 2)
        return normals

    def _interpolate(self, values, samples):
        return barycentric_interpolation_1d(
            self._pts[0, :], self._bary_weights, values, samples)


def partial_deriv(deriv_mats, quantity, dd, idx=None):
    if idx is None:
        return torch.linalg.multi_dot((deriv_mats[dd], quantity))
    return torch.linalg.multi_dot((deriv_mats[dd][idx], quantity))


def high_order_partial_deriv(order, deriv_mats, quantity, dd, idx=None):
    Dmat = torch.linalg.multi_dot([deriv_mats[dd] for ii in range(order)])
    if idx is None:
        return torch.linalg.multi_dot((Dmat, quantity))
    return torch.linalg.multi_dot((Dmat[idx], quantity))


def laplace(pderivs, quantity):
    vals = 0
    for dd in range(len(pderivs)):
        vals += pderivs[dd](pderivs[dd](quantity))
    return vals


def grad(pderivs, quantity, idx=None):
    vals = []
    for dd in range(len(pderivs)):
        vals.append(pderivs[dd](quantity, idx=idx)[:, None])
    return torch.hstack(vals)


def div(pderivs, quantities):
    vals = 0
    assert quantities.shape[1] == len(pderivs)
    for dd in range(len(pderivs)):
        vals += pderivs[dd](quantities[:, dd])
    return vals


def dot(quantities1, quantities2):
    vals = 0
    assert quantities1.shape[1] == quantities2.shape[1]
    vals = torch.sum(quantities1*quantities2, dim=1)
    return vals


class CanonicalCollocationMesh():
    def __init__(self, orders, basis_types=None):
        if len(orders) > 2:
            raise ValueError("Only 1D and 2D meshes supported")
        self.nphys_vars = len(orders)
        self._basis_types = self._get_basis_types(self.nphys_vars, basis_types)
        self._orders = orders
        self._canonical_domain_bounds = self._get_canonical_domain_bounds(
            self.nphys_vars, self._basis_types)
        (self._canonical_mesh_pts_1d, self._canonical_deriv_mats_1d,
         self._canonical_mesh_pts_1d_baryc_weights, self._canonical_mesh_pts,
         self._canonical_deriv_mats) = self._form_derivative_matrices()

        self._bndrys = self._form_boundaries()
        self._bndry_indices = self._determine_boundary_indices()
        self.nunknowns = self._canonical_mesh_pts.shape[1]
        self._partial_derivs = [partial(self.partial_deriv, dd=dd)
                                for dd in range(self.nphys_vars)]

    @staticmethod
    def _get_basis_types(nphys_vars, basis_types):
        if basis_types is None:
            basis_types = ["C"]*(nphys_vars)
        if len(basis_types) != nphys_vars:
            raise ValueError("Basis type must be specified for each dimension")
        return basis_types


    @staticmethod
    def _get_canonical_domain_bounds(nphys_vars, basis_types):
        canonical_domain_bounds = np.tile([-1, 1], nphys_vars)
        for ii in range(nphys_vars):
            if basis_types[ii] == "F":
                canonical_domain_bounds[2*ii:2*ii+2] = [0, 2*np.pi]
        return canonical_domain_bounds

    @staticmethod
    def _form_1d_derivative_matrices(order, basis_type):
        if basis_type == "C":
            return chebyshev_derivative_matrix(order)
        if basis_type == "F":
            return fourier_derivative_matrix(order)
        raise Exception(f"Basis type {basis_type} provided not supported")

    def _form_derivative_matrices(self):
        canonical_mesh_pts_1d, canonical_deriv_mats_1d = [], []
        for ii in range(self.nphys_vars):
            mpts, der_mat = self._form_1d_derivative_matrices(
                self._orders[ii], self._basis_types[ii])
            canonical_mesh_pts_1d.append(mpts)
            canonical_deriv_mats_1d.append(der_mat)

        canonical_mesh_pts_1d_baryc_weights = [
            compute_barycentric_weights_1d(xx) for xx in canonical_mesh_pts_1d]

        if self.nphys_vars == 1:
            canonical_deriv_mats = [canonical_deriv_mats_1d[0]]
        else:
            # assumes that 2d-mesh_pts varies in x1 faster than x2,
            # e.g. points are
            # [[x11,x21],[x12,x21],[x13,x12],[x11,x22],[x12,x22],...]
            canonical_deriv_mats = [
                np.kron(np.eye(self._orders[1]+1), canonical_deriv_mats_1d[0]),
                np.kron(canonical_deriv_mats_1d[1], np.eye(self._orders[0]+1))]
        canonical_deriv_mats = [torch.tensor(mat, dtype=torch.double)
                                for mat in canonical_deriv_mats]
        canonical_mesh_pts = cartesian_product(canonical_mesh_pts_1d)

        return (canonical_mesh_pts_1d, canonical_deriv_mats_1d,
                canonical_mesh_pts_1d_baryc_weights,
                canonical_mesh_pts, canonical_deriv_mats)

    def _form_boundaries(self):
        if self.nphys_vars == 1:
            return [Canonical1DMeshBoundary(name) for name in ["left", "right"]]
        return [Canonical2DMeshBoundary(name, self._orders[int(ii < 2)])
            for ii, name in enumerate(["left", "right", "bottom", "top"])]

    def _determine_boundary_indices(self):
        bndry_indices = [[] for ii in range(2*self.nphys_vars)]
        for ii in range(2*self.nphys_vars):
            bndry_indices[ii] = self._bndrys[ii].samples_on_boundary(
                self._canonical_mesh_pts)
        return bndry_indices

    def interpolate(self, values, eval_samples):
        if eval_samples.ndim == 1:
            eval_samples = eval_samples[None, :]
            assert eval_samples.shape[1] == self.nunknowns
        if values.ndim == 1:
            values = values[:, None]
            assert values.ndim == 2
        return self._interpolate(values, eval_samples)

    def _interpolate(self, values, canonical_eval_samples):
        if np.all([t == "C" for t in self._basis_types]):
            return self._cheby_interpolate(
                self._canonical_mesh_pts_1d,
                self._canonical_mesh_pts_1d_baryc_weights, values,
                canonical_eval_samples)
        if np.all([t == "F" for t in self._basis_types]):
            return self._fourier_interpolate(values, canonical_eval_samples)
        raise ValueError("Mixed basis not currently supported")

    def _get_lagrange_basis_mat(self, canonical_abscissa_1d,
                                canonical_eval_samples):
        if self.nphys_vars == 1:
            return torch.as_tensor(lagrange_polynomial_derivative_matrix_1d(
                canonical_eval_samples[0, :], canonical_abscissa_1d[0])[1])

        from pyapprox.pde.spectralcollocation.spectral_collocation import (
            lagrange_polynomial_basis_matrix_2d)
        return torch.as_tensor(lagrange_polynomial_basis_matrix_2d(
            canonical_eval_samples, canonical_abscissa_1d))

    def _cheby_interpolate(self, canonical_abscissa_1d,
                           canonical_barycentric_weights_1d, values,
                           canonical_eval_samples):
        # if type(values) != np.ndarray:
        #     values = values.detach().numpy()
        # interp_vals = multivariate_barycentric_lagrange_interpolation(
        #     canonical_eval_samples, canonical_abscissa_1d,
        #     canonical_barycentric_weights_1d, values,
        #     np.arange(self.nphys_vars))
        values = torch.as_tensor(values)
        basis_mat = self._get_lagrange_basis_mat(
            canonical_abscissa_1d, canonical_eval_samples)
        interp_vals = torch.linalg.multi_dot((basis_mat, values))
        return interp_vals

    def _fourier_interpolate(self, values, canonical_eval_samples):
        if type(values) != np.ndarray:
            values = values.detach().numpy()
            basis_vals = [
                fourier_basis(o, s)
            for o, s in zip(self._orders, canonical_eval_samples)]
        if self.nphys_vars == 1:
            return basis_vals[0].dot(values)
        return (basis_vals[0]*basis_vals[1]).dot(values)

    def _create_plot_mesh_1d(self, nplot_pts_1d):
        if nplot_pts_1d is None:
            return self._canonical_mesh_pts_1d[0]
        return np.linspace(
            self._canonical_domain_bounds[0],
            self._canonical_domain_bounds[1], nplot_pts_1d)

    def _plot_1d(self, mesh_values, nplot_pts_1d=None, ax=None,
                 **kwargs):
        plot_mesh = self._create_plot_mesh_1d(nplot_pts_1d)
        interp_vals = self.interpolate(mesh_values, plot_mesh[None, :])
        return ax.plot(plot_mesh, interp_vals, **kwargs)

    def _create_plot_mesh_2d(self, nplot_pts_1d):
        return get_meshgrid_samples(
            self._canonical_domain_bounds, nplot_pts_1d)

    def _plot_2d(self, mesh_values, nplot_pts_1d=100, ncontour_levels=20,
                 ax=None):
        X, Y, pts = self._create_plot_mesh_2d(nplot_pts_1d)
        Z = self._interpolate(mesh_values, pts)
        triang = tri.Triangulation(pts[0], pts[1])
        x = pts[0, triang.triangles].mean(axis=1)
        y = pts[1, triang.triangles].mean(axis=1)
        can_pts = self._map_samples_to_canonical_domain(
            np.vstack((x[None, :], y[None, :])))
        mask = np.where((can_pts[0] >= -1) & (can_pts[0] <= 1) &
                        (can_pts[1] >= -1) & (can_pts[1] <= 1), 0, 1)
        triang.set_mask(mask)
        return ax.tricontourf(
            triang, Z[:, 0],
            levels=np.linspace(Z.min(), Z.max(), ncontour_levels))

    def plot(self, mesh_values, nplot_pts_1d=None, ax=None, **kwargs):
        if ax is None:
            ax = plt.subplots(1, 1, figsize=(8, 6))[1]
        if self.nphys_vars == 1:
            return self._plot_1d(
                mesh_values, nplot_pts_1d, ax, **kwargs)
        if nplot_pts_1d is None:
            raise ValueError("nplot_pts_1d must be not None for 2D plot")
        return self._plot_2d(
            mesh_values, nplot_pts_1d, 30, ax=ax)

    def _get_quadrature_rule(self):
        quad_rules = [
            gauss_jacobi_pts_wts_1D(o+2, 0, 0) for o in self._orders]
        canonical_xquad = cartesian_product([q[0] for q in quad_rules])
        canonical_wquad = outer_product([q[1] for q in quad_rules])
        return canonical_xquad, canonical_wquad

    def integrate(self, mesh_values):
        xquad, wquad = self._get_quadrature_rule()
        return self.interpolate(mesh_values, xquad)[:, 0].dot(
            torch.tensor(wquad))

    def laplace(self, quantity):
        return laplace(self._partial_derivs, quantity)

    def partial_deriv(self, quantity, dd, idx=None):
        return partial_deriv(self._canonical_deriv_mats, quantity, dd, idx)

    def high_order_partial_deriv(self, order, quantity, dd, idx=None):
        return high_order_partial_deriv(
            order, self._canonical_deriv_mats, quantity, dd, idx)

    def grad(self, quantity, idx=None):
        return grad(self._partial_derivs, quantity, idx)

    def div(self, quantities):
        return div(self._partial_derivs, quantities)

    def dot(self, quantities1, quantities2):
        return dot(quantities1, quantities2)

    # TODO remove self._bdnry_conds from mesh
    # and make property of residual or solver base class
    def _apply_custom_boundary_conditions_to_residual(
            self, bndry_conds, residual, sol):
        for ii, bndry_cond in enumerate(bndry_conds):
            if bndry_cond[1] == "C":
                if self._basis_types[ii//2] == "F":
                    msg = "Cannot enforce non-periodic boundary conditions "
                    msg += "when using a Fourier basis"
                    raise ValueError(msg)
                idx = self._bndry_indices[ii]
                bndry_vals = bndry_cond[0](self.mesh_pts[:, idx])[:, 0]
                bndry_lhs = bndry_cond[2](sol, idx, self, ii)
                assert bndry_lhs.ndim == 1
                residual[idx] = (bndry_lhs-bndry_vals)
        return residual

    def _apply_dirichlet_boundary_conditions_to_residual(
            self, bndry_conds, residual, sol):
        for ii, bndry_cond in enumerate(bndry_conds):
            if bndry_cond[1] == "D":
                if self._basis_types[ii//2] == "F":
                    msg = "Cannot enforce non-periodic boundary conditions "
                    msg += "when using a Fourier basis"
                    raise ValueError(msg)
                idx = self._bndry_indices[ii]
                bndry_vals = bndry_cond[0](self.mesh_pts[:, idx])[:, 0]
                residual[idx] = sol[idx]-bndry_vals
        return residual

    def _apply_periodic_boundary_conditions_to_residual(
            self, bndry_conds, residual, sol):
        for ii in range(len(bndry_conds)//2):
            if (self._basis_types[ii] == "C" and bndry_conds[2*ii][1] == "P"):
                idx1 = self._bndry_indices[2*ii]
                idx2 = self._bndry_indices[2*ii+1]
                residual[idx1] = sol[idx1]-sol[idx2]
                residual[idx2] = (
                    self.partial_deriv(sol, ii//2, idx1) -
                    self.partial_deriv(sol, ii//2, idx2))
        return residual

    def _apply_neumann_and_robin_boundary_conditions_to_residual(
            self, bndry_conds, residual, sol):
        for ii, bndry_cond in enumerate(bndry_conds):
            if bndry_cond[1] == "N" or bndry_cond[1] == "R":
                if self._basis_types[ii//2] == "F":
                    msg = "Cannot enforce non-periodic boundary conditions "
                    msg += "when using a Fourier basis"
                    raise ValueError(msg)
                idx = self._bndry_indices[ii]
                bndry_vals = bndry_cond[0](self.mesh_pts[:, idx])[:, 0]
                normal_vals = self._bndrys[ii].normals(self.mesh_pts[:, idx])
                # warning flux is not dependent on diffusivity (
                # diffusion equation not the usual boundary formulation used
                # for spectral Galerkin methods)
                flux_vals = self.grad(sol, idx)
                residual[idx] = self.dot(flux_vals, normal_vals)-bndry_vals
                if bndry_cond[1] == "R":
                    residual[idx] += bndry_cond[2]*sol[idx]
        return residual

    def _apply_boundary_conditions_to_residual(
            self, bndry_conds, residual, sol):
        residual = self._apply_dirichlet_boundary_conditions_to_residual(
            bndry_conds, residual, sol)
        residual = (
            self._apply_neumann_and_robin_boundary_conditions_to_residual(
                bndry_conds, residual, sol))
        residual = (self._apply_periodic_boundary_conditions_to_residual(
            bndry_conds, residual, sol))
        residual = (self._apply_custom_boundary_conditions_to_residual(
            bndry_conds, residual, sol))
        return residual

    def _dmat(self, dd):
        dmat = 0
        for ii in range(self.nphys_vars):
            if self._transform_inv_derivs[dd][ii] is not None:
                scale = self._deriv_scale(dd, ii, None)
                if scale is not None:
                    dmat += scale[:, None]*self._canonical_deriv_mats[ii]
        return dmat

    def _apply_dirichlet_boundary_conditions(
            self, bndry_conds, residual, jac, sol):
        # needs to have indices as argument so this fucntion can be used
        # when setting boundary conditions for forward and adjoint solves
        for ii, bndry_cond in enumerate(bndry_conds):
            if bndry_cond[1] != "D":
                continue
            idx = self._bndry_indices[ii]
            jac[idx, :] = 0
            jac[idx, idx] = 1
            bndry_vals = bndry_cond[0](self.mesh_pts[:, idx])[:, 0]
            residual[idx] = sol[idx]-bndry_vals
        return residual, jac

    def _apply_neumann_and_robin_boundary_conditions(
            self, bndry_conds, residual, jac, sol):
        for ii, bndry_cond in enumerate(bndry_conds):
            if bndry_cond[1] != "N" and bndry_cond[1] != "R":
                continue
            idx = self._bndry_indices[ii]
            normal_vals = self._bndrys[ii].normals(
                self.mesh_pts[:, idx])
            grad_vals = [normal_vals[:, dd:dd+1]*self._dmat(dd)[idx]
                         for dd in range(self.nphys_vars)]
            # (D2*u)*n2+D2*u*n2
            jac[idx] = sum(grad_vals)
            bndry_vals = bndry_cond[0](self.mesh_pts[:, idx])[:, 0]
            residual[idx] = torch.linalg.multi_dot((jac[idx], sol))-bndry_vals
            if bndry_cond[1] == "R":
                jac[idx, idx] += bndry_cond[2]
                residual[idx] += bndry_cond[2]*sol[idx]
        # assert False
        return residual, jac

    def _apply_periodic_boundary_conditions(
            self, bndry_conds, residual, jac, sol):
        for ii in range(len(bndry_conds)//2):
            if (self._basis_types[ii] == "C" and bndry_conds[2*ii][1] == "P"):
                idx1 = self._bndry_indices[2*ii]
                idx2 = self._bndry_indices[2*ii+1]
                jac[idx1, :] = 0
                jac[idx1, idx1] = 1
                jac[idx1, idx2] = -1
                jac[idx2] = self._dmat(ii//2)[idx1]-self._dmat(ii//2)[idx2]
                residual[idx1] = sol[idx1]-sol[idx2]
                residual[idx2] = (
                    torch.linalg.multi_dot((self._dmat(ii//2)[idx1], sol)) -
                    torch.linalg.multi_dot((self._dmat(ii//2)[idx2], sol)))
        return residual, jac

    def _apply_boundary_conditions(self, bndry_conds, residual, jac, sol):
        if jac is None:
            return self._apply_boundary_conditions_to_residual(
                bndry_conds, residual, sol), None
        residual, jac = self._apply_dirichlet_boundary_conditions(
                bndry_conds, residual, jac, sol)
        residual, jac = self._apply_periodic_boundary_conditions(
            bndry_conds, residual, jac, sol)
        residual, jac = self._apply_neumann_and_robin_boundary_conditions(
            bndry_conds, residual, jac, sol)
        return residual, jac


class TransformedCollocationMesh(CanonicalCollocationMesh):
    def __init__(self, orders, transform, transform_inv,
                 transform_inv_derivs, trans_bndry_normals, basis_types=None):

        super().__init__(orders, basis_types)

        self._transform = transform
        self._transform_inv = transform_inv
        self._transform_inv_derivs = transform_inv_derivs
        if len(trans_bndry_normals) != 2*self.nphys_vars:
            raise ValueError(
                "Must provide normals for each transformed boundary")
        self._trans_bndry_normals = trans_bndry_normals

        self.mesh_pts = self._map_samples_from_canonical_domain(
            self._canonical_mesh_pts)

        self._bndrys = self._transform_boundaries()

    def _transform_boundaries(self):
        if self.nphys_vars == 1:
            return self._bndrys
        for ii, name in enumerate(["left", "right", "bottom", "top"]):
            active_var = int(ii > 2)
            idx = self._bndry_indices[ii]
            # bndry_deriv_vals = self._canonical_deriv_mats_1d[active_var].dot(
            #     self.mesh_pts[active_var, idx])
            # self._bndrys[ii] = Transformed2DMeshBoundary(
            #     name, self._orders[int(ii < 2)], bndry_deriv_vals,
            #     self._bndrys[ii]._tol)
            self._bndrys[ii] = Transformed2DMeshBoundary(
                name, self._orders[int(ii < 2)], self._trans_bndry_normals[ii],
                self._bndrys[ii]._tol)
        return self._bndrys

    def _map_samples_from_canonical_domain(self, canonical_samples):
        return self._transform(canonical_samples)

    def _map_samples_to_canonical_domain(self, samples):
        return self._transform_inv(samples)

    def _interpolate(self, values, eval_samples):
        canonical_eval_samples = self._map_samples_to_canonical_domain(
            eval_samples)
        return super()._interpolate(values, canonical_eval_samples)

    def _deriv_scale(self, dd, ii, idx=None):
        if self._transform_inv_derivs[dd][ii] == 0:
            return None

        if self._transform_inv_derivs[dd][ii] == 1:
            return 1

        if idx is not None:
            return self._transform_inv_derivs[dd][ii](
                self.mesh_pts[:, idx])
        
        return self._transform_inv_derivs[dd][ii](
            self.mesh_pts)

    def partial_deriv(self, quantity, dd, idx=None):
        # dq/du = dq/dx * dx/du + dq/dy * dy/du
        assert quantity.ndim == 1
        vals = 0
        for ii in range(self.nphys_vars):
            scale = self._deriv_scale(dd, ii, idx)
            if scale is not None:
                vals += scale*super().partial_deriv(quantity, ii, idx)
        return vals

    def _create_plot_mesh_1d(self, nplot_pts_1d):
        if nplot_pts_1d is None:
            return self.mesh_pts[0, :]
        return np.linspace(
            self._domain_bounds[0], self._domain_bounds[1], nplot_pts_1d)

    def _create_plot_mesh_2d(self, nplot_pts_1d):
        X, Y, pts = super()._create_plot_mesh_2d(nplot_pts_1d)
        pts = self._map_samples_from_canonical_domain(pts)
        return X, Y, pts


def _derivatives_map_hypercube(current_range, new_range, samples):
    current_len = current_range[1]-current_range[0]
    new_len = new_range[1]-new_range[0]
    map_derivs = torch.full(
        (samples.shape[1], ), (new_len/current_len), dtype=torch.double)
    return map_derivs


class CartesianProductCollocationMesh(TransformedCollocationMesh):
    def __init__(self, domain_bounds, orders, basis_types=None):
        nphys_vars = len(orders)
        self._domain_bounds = np.asarray(domain_bounds)
        basis_types = self._get_basis_types(nphys_vars, basis_types)
        canonical_domain_bounds = (
            CanonicalCollocationMesh._get_canonical_domain_bounds(
                nphys_vars, basis_types))
        transform = partial(
            _map_hypercube_samples,
            current_ranges=canonical_domain_bounds,
            new_ranges=self._domain_bounds)
        transform_inv = partial(
            _map_hypercube_samples,
            current_ranges=self._domain_bounds,
            new_ranges=canonical_domain_bounds)
        transform_inv_derivs = []
        for ii in range(nphys_vars):
            transform_inv_derivs.append([0 for jj in range(nphys_vars)])
            transform_inv_derivs[ii][ii] = partial(
                _derivatives_map_hypercube,
                self._domain_bounds[2*ii:2*ii+2],
                canonical_domain_bounds[2*ii:2*ii+2])
        trans_normals = [None]*(nphys_vars*2)
        super().__init__(
            orders, transform, transform_inv,
            transform_inv_derivs, trans_normals, basis_types=basis_types)

    def high_order_partial_deriv(self, order, quantity, dd, idx=None):
        # value of xx does not matter for cartesian_product meshes
        xx = np.zeros((1, 1))
        deriv_mats = [tmp[0]*tmp[1][ii](xx)[0] for ii, tmp in enumerate(
            zip(self._canonical_deriv_mats, self._transform_inv_derivs))]
        return high_order_partial_deriv(
            order, self._canonical_deriv_mats, quantity, dd, idx)

    def _get_quadrature_rule(self):
        canonical_xquad, canonical_wquad = super()._get_quadrature_rule()
        xquad = self._map_samples_from_canonical_domain(canonical_xquad)
        wquad = canonical_wquad/np.prod(
            self._domain_bounds[1::2]-self._domain_bounds[::2])
        return xquad, wquad


class VectorMesh():
    def __init__(self, meshes):
        self._meshes = meshes
        self.nunknowns = sum([m.mesh_pts.shape[1] for m in self._meshes])
        self.nphys_vars = self._meshes[0].nphys_vars

    def split_quantities(self, vector):
        cnt = 0
        split_vector = []
        for ii in range(len(self._meshes)):
            ndof = self._meshes[ii].mesh_pts.shape[1]
            split_vector.append(vector[cnt:cnt+ndof])
            cnt += ndof
        return split_vector

    def _apply_boundary_conditions_to_residual(self, bndry_conds, residual, sol):
        split_sols = self.split_quantities(sol)
        split_residual = self.split_quantities(residual)
        for ii, mesh in enumerate(self._meshes):
            split_residual[ii] = mesh._apply_boundary_conditions_to_residual(
                bndry_conds[ii], split_residual[ii], split_sols[ii])
        return torch.cat(split_residual)

    def _zero_boundary_equations(self, mesh, bndry_conds, jac):
        for ii in range(len(bndry_conds)):
            if bndry_conds[ii][1] is not None:
                jac[mesh._bndry_indices[ii], :] = 0
        return jac

    def _apply_boundary_conditions_to_residual(
            self, bndry_conds, residual, sol):
        split_sols = self.split_quantities(sol)
        split_residual = self.split_quantities(residual)
        for ii, mesh in enumerate(self._meshes):
            split_residual[ii] = (
                mesh._apply_boundary_conditions(
                    bndry_conds[ii], split_residual[ii], None,
                    split_sols[ii]))[0]
        return torch.cat(split_residual), None

    def _apply_boundary_conditions(self, bndry_conds, residual, jac, sol):
        if jac is None:
            return self._apply_boundary_conditions_to_residual(
                bndry_conds, residual, sol)

        split_sols = self.split_quantities(sol)
        split_residual = self.split_quantities(residual)
        split_jac = self.split_quantities(jac)
        for ii, mesh in enumerate(self._meshes):
            split_jac[ii] = self._zero_boundary_equations(
                mesh, bndry_conds[ii], split_jac[ii])
            ssjac = self.split_quantities(split_jac[ii].T)
            split_residual[ii], tmp = (
                mesh._apply_boundary_conditions(
                    bndry_conds[ii], split_residual[ii], ssjac[ii].T,
                    split_sols[ii]))
            ssjac = [s.T for s in ssjac]
            ssjac[ii] = tmp
            split_jac[ii] = torch.hstack(ssjac)
        return torch.cat(split_residual), torch.vstack(split_jac)

    def interpolate(self, sol_vals, xx):
        Z = []
        for ii in range(len(self._meshes)):
            Z.append(self._meshes[ii].interpolate(sol_vals[ii], xx))
        return Z

    def integrate(self, sol_vals):
        Z = []
        for ii in range(len(self._meshes)):
            Z.append(self._meshes[ii].integrate(sol_vals[ii]))
        return Z

    def plot(self, sol_vals, nplot_pts_1d=50, axs=None, **kwargs):
        if axs is None:
            fig, axs = plt.subplots(
                1, self.nphys_vars+1, figsize=(8*(len(sol_vals)), 6))
        if self._meshes[0].nphys_vars == 1:
            xx = np.linspace(
                *self._meshes[0]._domain_bounds, nplot_pts_1d)[None, :]
            Z = self.interpolate(sol_vals, xx)
            objs = []
            for ii in range(2):
                obj, = axs[ii].plot(xx[0, :], Z[ii], **kwargs)
                objs.append(obj)
            return objs
        X, Y, pts = get_meshgrid_samples(
            self._meshes[0]._domain_bounds, nplot_pts_1d)
        Z = self.interpolate(sol_vals, pts)
        objs = []
        for ii in range(len(Z)):
            obj = axs[ii].contourf(
                X, Y, Z[ii].reshape(X.shape),
                levels=np.linspace(Z[ii].min(), Z[ii].max(), 20))
            objs.append(obj)
        return objs


class CanonicalInteriorCollocationMesh(CanonicalCollocationMesh):
    def __init__(self, orders):
        super().__init__(orders, None)
        
        self._canonical_deriv_mats_alt = (
            self._form_derivative_matrices_alt())

    def _apply_boundary_conditions_to_residual(self, bndry_conds, residual,
                                               sol):
        return residual

    def _form_canonical_deriv_matrices(self, canonical_mesh_pts_1d):
        eval_samples = cartesian_product(
            [-np.cos(np.linspace(0, np.pi, o+1)) for o in self._orders])
        if self.nphys_vars == 2:
            canonical_deriv_mats, __, canonical_mesh_pts = (
                lagrange_polynomial_derivative_matrix_2d(
                    eval_samples, canonical_mesh_pts_1d))
            return canonical_deriv_mats, canonical_mesh_pts

        return [lagrange_polynomial_derivative_matrix_1d(
            eval_samples[0], canonical_mesh_pts_1d[0])[0]], np.atleast_1d(
                canonical_mesh_pts_1d)

    def _form_derivative_matrices(self):
        # will work but divergence condition is only satisfied on interior
        # so if want to drive flow with only boundary conditions on velocity
        # it will not work
        canonical_mesh_pts_1d = [
            -np.cos(np.linspace(0, np.pi, o+1))[1:-1] for o in self._orders]
        canonical_mesh_pts_1d_baryc_weights = [
            compute_barycentric_weights_1d(xx) for xx in canonical_mesh_pts_1d]
        canonical_deriv_mats, canonical_mesh_pts = (
            self._form_canonical_deriv_matrices(canonical_mesh_pts_1d))
        canonical_deriv_mats = [
            torch.tensor(mat, dtype=torch.double) for mat in canonical_deriv_mats]
        return (canonical_mesh_pts_1d, None,
                canonical_mesh_pts_1d_baryc_weights,
                canonical_mesh_pts, canonical_deriv_mats)

    def _form_derivative_matrices_alt(self):
        canonical_mesh_pts_1d = [
            -np.cos(np.linspace(0, np.pi, o+1))[1:-1] for o in self._orders]
        if self.nphys_vars == 2:
            canonical_deriv_mats_alt = (
                lagrange_polynomial_derivative_matrix_2d(
                    cartesian_product(canonical_mesh_pts_1d),
                    [-np.cos(np.linspace(0, np.pi, o+1))
                     for o in self._orders])[0])
        else:
            canonical_deriv_mats_alt = [
                lagrange_polynomial_derivative_matrix_1d(
                    canonical_mesh_pts_1d[0],
                    -np.cos(np.linspace(0, np.pi, self._orders[0]+1)))[0]]
        canonical_deriv_mats_alt = [
            torch.tensor(mat, dtype=torch.double)
            for mat in canonical_deriv_mats_alt]
        return canonical_deriv_mats_alt

    def _get_canonical_deriv_mats(self, quantity):
        if quantity.shape[0] == self.nunknowns:
            return self._canonical_deriv_mats
        elif quantity.shape[0] == self._canonical_deriv_mats_alt[0].shape[1]:
            return self._canonical_deriv_mats_alt
        raise RuntimeError("quantity is the wrong shape")

    def _determine_boundary_indices(self):
        self._boundary_indices = None

    def partial_deriv(self, quantity, dd, idx=None):
        return partial_deriv(
            self._get_canonical_deriv_mats(quantity), quantity, dd, idx)


class TransformedInteriorCollocationMesh(CanonicalInteriorCollocationMesh):
    def __init__(self, orders, transform, transform_inv, transform_inv_derivs):

        super().__init__(orders)
        
        self._transform = transform
        self._transform_inv = transform_inv
        self._transform_inv_derivs = transform_inv_derivs

        self.mesh_pts = self._map_samples_from_canonical_domain(
            self._canonical_mesh_pts)

        self.mesh_pts_alt = self._map_samples_from_canonical_domain(
            cartesian_product(
                [-np.cos(np.linspace(0, np.pi, o+1)) for o in self._orders]))

    def _map_samples_from_canonical_domain(self, canonical_samples):
        return self._transform(canonical_samples)

    def _map_samples_to_canonical_domain(self, samples):
        return self._transform_inv(samples)

    def _interpolate(self, values, eval_samples):
        canonical_eval_samples = self._map_samples_to_canonical_domain(
            eval_samples)
        return super()._interpolate(values, canonical_eval_samples)

    def _deriv_scale(self, quantity, dd, ii, idx=None):
        if (self._transform_inv_derivs[dd][ii] == 0 or
            self._transform_inv_derivs[dd][ii] is None):
            return None

        if self._transform_inv_derivs[dd][ii] == 1:
            return 1

        if quantity.shape[0] == self.mesh_pts.shape[1]:
            mesh_pts = self.mesh_pts_alt
        elif quantity.shape[0] == self.mesh_pts_alt.shape[1]:
            mesh_pts = self.mesh_pts
        else:
            RuntimeError()
            
        if idx is not None:
            return self._transform_inv_derivs[dd][ii](
                mesh_pts[:, idx])
        return self._transform_inv_derivs[dd][ii](
            mesh_pts)

    def partial_deriv(self, quantity, dd, idx=None):
        # dq/du = dq/dx * dx/du + dq/dy * dy/du
        assert quantity.ndim == 1
        vals = 0
        for ii in range(self.nphys_vars):
            scale = self._deriv_scale(quantity, dd, ii, idx)
            if scale is not None:
                vals += scale*super().partial_deriv(quantity, ii, idx)
        return vals

    def _dmat(self, quantity1, dd):
        dmat = 0
        for ii in range(self.nphys_vars):
            if self._transform_inv_derivs[dd][ii] is not None:
                scale = self._deriv_scale(quantity1, dd, ii, None)
                if scale is not None:
                    dmat += scale[:, None]*self._get_canonical_deriv_mats(
                        quantity1)[ii]
        return dmat
    
    # def partial_deriv(self, quantity, dd, idx=None):
    #     # dq/du = dq/dx * dx/du + dq/dy * dy/du
    #     vals = 0
    #     for ii in range(self.nphys_vars):
    #         if self._transform_inv_derivs[dd][ii] is not None:
    #             if idx is not None:
    #                 scale = self._transform_inv_derivs[dd][ii](
    #                     self._canonical_mesh_pts[:, idx])
    #             else:
    #                  scale = self._transform_inv_derivs[dd][ii](
    #                      self._canonical_mesh_pts)
    #             scale = self._transform_inv_derivs[dd][ii](
    #                 self._canonical_mesh_pts)
    #             if idx is not None:
    #                 scale = scale[idx]
    #             print(idx, scale.shape, quantity.shape)
    #             print(super().partial_deriv(quantity, ii, idx).shape)
    #             vals += scale*super().partial_deriv(quantity, ii, idx)
    #         # else: scale is zero
    #     return vals


class InteriorCartesianProductCollocationMesh(TransformedInteriorCollocationMesh):
    def __init__(self, domain_bounds, orders):
        nphys_vars = len(orders)
        self._domain_bounds = np.asarray(domain_bounds)
        basis_types = self._get_basis_types(nphys_vars, None)
        canonical_domain_bounds = (
            CanonicalCollocationMesh._get_canonical_domain_bounds(
                nphys_vars, basis_types))
        transform = partial(
            _map_hypercube_samples,
            current_ranges=canonical_domain_bounds,
            new_ranges=self._domain_bounds)
        transform_inv = partial(
            _map_hypercube_samples,
            current_ranges=self._domain_bounds,
            new_ranges=canonical_domain_bounds)
        transform_inv_derivs = []
        for ii in range(nphys_vars):
            transform_inv_derivs.append([None for jj in range(nphys_vars)])
            transform_inv_derivs[ii][ii] = partial(
                _derivatives_map_hypercube,
                self._domain_bounds[2*ii:2*ii+2],
                canonical_domain_bounds[2*ii:2*ii+2])
        super().__init__(
            orders, transform, transform_inv, transform_inv_derivs)

def vertical_transform_2D_mesh(xdomain_bounds, bed_fun, surface_fun,
                               canonical_samples):
    samples = np.empty_like(canonical_samples)
    xx, yy = canonical_samples[0], canonical_samples[1]
    samples[0] = (xx+1)/2*(
        xdomain_bounds[1]-xdomain_bounds[0])+xdomain_bounds[0]
    bed_vals = bed_fun(samples[0:1])[:, 0]
    samples[1] = (yy+1)/2*(surface_fun(samples[0:1])[:, 0]-bed_vals)+bed_vals
    return samples


def vertical_transform_2D_mesh_inv(xdomain_bounds, bed_fun, surface_fun,
                                   samples):
    canonical_samples = np.empty_like(samples)
    uu, vv = samples[0], samples[1]
    canonical_samples[0] = 2*(uu-xdomain_bounds[0])/(
        xdomain_bounds[1]-xdomain_bounds[0])-1
    bed_vals = bed_fun(samples[0:1])[:, 0]
    canonical_samples[1] = 2*(samples[1]-bed_vals)/(
        surface_fun(samples[0:1])[:, 0]-bed_vals)-1
    return canonical_samples


def vertical_transform_2D_mesh_inv_dxdu(xdomain_bounds, samples):
    return np.full(samples.shape[1], 2/(xdomain_bounds[1]-xdomain_bounds[0]))


def vertical_transform_2D_mesh_inv_dydu(
        bed_fun, surface_fun, bed_grad_u, surf_grad_u, samples):
    surf_vals = surface_fun(samples[:1])[:, 0]
    bed_vals = bed_fun(samples[:1])[:, 0]
    return 2*(bed_grad_u(samples[:1])[:, 0]*(samples[1]-surf_vals) +
              surf_grad_u(samples[:1])[:, 0]*(bed_vals-samples[1]))/(
                  surf_vals-bed_vals)**2


def vertical_transform_2D_mesh_inv_dxdv(samples):
    return np.zeros(samples.shape[1])


def vertical_transform_2D_mesh_inv_dydv(bed_fun, surface_fun, samples):
    surf_vals = surface_fun(samples[:1])[:, 0]
    bed_vals = bed_fun(samples[:1])[:, 0]
    return 2/(surf_vals-bed_vals)
