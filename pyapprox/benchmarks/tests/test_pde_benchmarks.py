import unittest
import numpy as np
from functools import partial
from pyapprox.optimization.pya_minimize import pyapprox_minimize

from pyapprox.benchmarks.pde_benchmarks import (
    _setup_inverse_advection_diffusion_benchmark,
    _setup_multi_index_advection_diffusion_benchmark)
from pyapprox.util.utilities import check_gradients, get_all_sample_combinations


class TestPDEBenchmarks(unittest.TestCase):

    def setUp(self):
        np.random.seed(1)

    def test_setup_inverse_advection_diffusion_benchmark(self):
        nobs = 20
        noise_std = 1e-8 #0.01  # make sure it does not dominate observed values
        length_scale = .5
        sigma = 1
        nvars = 10
        orders = [20, 20]

        inv_model, variable, true_params, noiseless_obs, obs = (
            _setup_inverse_advection_diffusion_benchmark(
                nobs, noise_std, length_scale, sigma, nvars, orders))

        # TODO add std to params list
        init_guess = variable.rvs(1)
        # init_guess = true_params
        errors = check_gradients(
            lambda zz: inv_model._eval(zz[:, 0], jac=True),
            True, init_guess, plot=False,
            fd_eps=np.logspace(-13, 0, 14)[::-1])
        assert errors[0] > 5e-2 and errors.min() < 3e-6

        jac = True
        opt_result = pyapprox_minimize(
            partial(inv_model._eval, jac=jac),
            init_guess,
            method="trust-constr", jac=jac,
            options={"verbose": 0, "gtol": 1e-6, "xtol": 1e-16})
        # print(opt_result.x)
        # print(true_params.T)
        print(opt_result.x-true_params.T)
        assert np.allclose(opt_result.x, true_params.T, atol=2e-5)

    def test_setup_multi_index_advection_diffusion_benchmark(self):
        length_scale = .1
        sigma = 1
        nvars = 5

        config_values = [2*np.arange(1, 11), 2*np.arange(1, 11)]
        model, variable = _setup_multi_index_advection_diffusion_benchmark(
            length_scale, sigma, nvars, config_values)
        print(variable.num_vars())

        nrandom_samples = 10
        random_samples = variable.rvs(nrandom_samples)

        import matplotlib.pyplot as plt
        # import torch
        # pde1 = model._model_ensemble.functions[-1]
        # mesh1 = pde1._fwd_solver.residual.mesh
        # kle_vals1 = pde1._kle(torch.as_tensor(random_samples[:, :1]))
        # pde2 = model._model_ensemble.functions[0]
        # mesh2 = pde2._fwd_solver.residual.mesh
        # kle_vals2 = pde2._kle(torch.as_tensor(random_samples[:, :1]))
        # kle_vals2 = mesh2.interpolate(kle_vals2, mesh1.mesh_pts)
        # im = mesh1.plot(
        #     kle_vals1-kle_vals2, 50,  ncontour_levels=30)
        # plt.colorbar(im)
        # fig, axs = plt.subplots(1, 2, figsize=(2*8, 6))
        # im1 = mesh1.plot(
        #     kle_vals1, 50,  ncontour_levels=30, ax=axs[0])
        # im2 = mesh1.plot(
        #     kle_vals2, 50,  ncontour_levels=30, ax=axs[1])
        # plt.colorbar(im1, ax=axs[0])
        # plt.colorbar(im2, ax=axs[1])
        # plt.show()
        
        config_samples = np.vstack([c[None, :] for c in config_values])
        samples = get_all_sample_combinations(random_samples, config_samples)
        values = model(samples)
        np.set_printoptions(precision=16)
        values = values.reshape((nrandom_samples, config_samples.shape[1]))
        qoi_means = values.mean(axis=0)
        rel_diffs = np.abs((qoi_means[-1]-qoi_means[:-1])/qoi_means[-1])
        print(rel_diffs)
        assert (rel_diffs.max() > 1e-1 and rel_diffs.min() < 3e-5)
        # ndof = (config_samples+1).prod(axis=0)
        # plt.loglog(
        #     ndof[:-1], np.abs((qoi_means[-1]-qoi_means[:-1])/qoi_means[-1]))
        # plt.show()
        

if __name__ == "__main__":
    pde_benchmarks_test_suite = unittest.TestLoader().loadTestsFromTestCase(
        TestPDEBenchmarks)
    unittest.TextTestRunner(verbosity=2).run(pde_benchmarks_test_suite)
