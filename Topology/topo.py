import numpy as onp
import jax
import jax.numpy as np
import os
import glob
import matplotlib.pyplot as plt


# Import JAX-FEM specific modules.
from jax_fem.problem import Problem
from jax_fem.solver import solver, ad_wrapper
from jax_fem.utils import save_sol
from jax_fem.generate_mesh import get_meshio_cell_type, Mesh, rectangle_mesh, box_mesh
from jax_fem.mma import optimize


# Define constitutive relationship. 
# Generally, JAX-FEM solves -div.(f(u_grad,alpha_1,alpha_2,...,alpha_N)) = b. 
# Here, we have f(u_grad,alpha_1,alpha_2,...,alpha_N) = sigma(u_grad, theta),
# reflected by the function 'stress'. The functions 'custom_init'and 'set_params' 
# override base class methods. In particular, set_params sets the design variable theta.
class LinearElasticity(Problem):
    def custom_init(self):
        # Override base class method.
        # Set up 'self.fe.flex_inds' so that location-specific TO can be realized.
        self.fe = self.fes[0]
        self.fe.flex_inds = np.arange(len(self.fe.cells))

    def get_tensor_map(self):
        def stress(u_grad, theta):
            # 3D elasticity tensor
            # Reference: https://en.wikipedia.org/wiki/Hooke%27s_law
            Emax = 70.e3
            Emin = 1e-3*Emax
            nu = 0.3
            penal = 3.
            E = Emin + (Emax - Emin)*theta[0]**penal
            
            epsilon = 0.5*(u_grad + u_grad.T)
            
            eps11 = epsilon[0, 0]
            eps22 = epsilon[1, 1]
            eps33 = epsilon[2, 2]
            eps12 = epsilon[0, 1]
            eps13 = epsilon[0, 2]
            eps23 = epsilon[1, 2]
            
            lmbda = E*nu/((1 + nu)*(1 - 2*nu))
            mu = E/(2*(1 + nu))
            
            sig11 = lmbda*(eps11 + eps22 + eps33) + 2*mu*eps11
            sig22 = lmbda*(eps11 + eps22 + eps33) + 2*mu*eps22
            sig33 = lmbda*(eps11 + eps22 + eps33) + 2*mu*eps33
            sig12 = 2*mu*eps12
            sig13 = 2*mu*eps13
            sig23 = 2*mu*eps23
            
            sigma = np.array([[sig11, sig12, sig13],
                            [sig12, sig22, sig23],
                            [sig13, sig23, sig33]])
            
            return sigma
        
        return stress

    def get_surface_maps(self):
        def surface_map(u, x):
            return np.array([0., 100., 0.])
        return [surface_map]

    def set_params(self, params):
        # Override base class method.
        full_params = np.ones((self.fe.num_cells, params.shape[1]))
        full_params = full_params.at[self.fe.flex_inds].set(params)
        thetas = np.repeat(full_params[:, None, :], self.fe.num_quads, axis=1)
        self.full_params = full_params
        self.internal_vars = [thetas]

    def compute_compliance(self, sol):
        # Surface integral
        boundary_inds = self.boundary_inds_list[0]
        _, nanson_scale = self.fe.get_face_shape_grads(boundary_inds)
        # (num_selected_faces, 1, num_nodes, vec) * # (num_selected_faces, num_face_quads, num_nodes, 1)    
        u_face = sol[self.fe.cells][boundary_inds[:, 0]][:, None, :, :] * self.fe.face_shape_vals[boundary_inds[:, 1]][:, :, :, None]
        u_face = np.sum(u_face, axis=2) # (num_selected_faces, num_face_quads, vec)
        # (num_cells, num_faces, num_face_quads, dim) -> (num_selected_faces, num_face_quads, dim)
        
        # subset_quad_points = self.get_physical_surface_quad_points(boundary_inds)

        subset_quad_points = self.physical_surface_quad_points[0]

        neumann_fn = self.get_surface_maps()[0]
        traction = -jax.vmap(jax.vmap(neumann_fn))(u_face, subset_quad_points) # (num_selected_faces, num_face_quads, vec)
        val = np.sum(traction * u_face * nanson_scale[:, :, None])
        return val


# Do some cleaning work. Remove old solution files.
data_path = os.path.join(os.path.dirname(__file__), 'data') 
files = glob.glob(os.path.join(data_path, f'vtk/*'))
for f in files:
    os.remove(f)


# Specify mesh-related information. We use first-order quadrilateral element.
ele_type = 'TET10'
cell_type = get_meshio_cell_type(ele_type)
data_dir = os.path.join(os.path.dirname(__file__), 'data')
Lx, Ly, Lz = 6., 3., 3.
Nx, Ny, Nz = 6., 3., 3.
meshio_mesh = box_mesh(Nx=Nx,
                       Ny=Ny,
                       Nz=Nz,
                       Lx=Lx,
                       Ly=Ly,
                       Lz=Lz,
                       data_dir=data_dir,
                       ele_type=ele_type)

#meshio_mesh = rectangle_mesh(Nx=60, Ny=30, domain_x=Lx, domain_y=Ly)
mesh = Mesh(meshio_mesh.points, meshio_mesh.cells_dict[cell_type])


# Define boundary conditions and values.
def fixed_location(point):
    return np.isclose(point[0], 0., atol=1e-5)
    
def load_location(point):
    #return np.logical_and(np.isclose(point[0], Lx, atol=1e-5), np.isclose(point[1], 0., atol=0.1*Ly + 1e-5))
    return np.isclose(point[0], Lx, atol=1e-5)


def dirichlet_val(point):
    return 0.

dirichlet_bc_info = [[fixed_location]*3, [0, 1, 2], [dirichlet_val]*3]

location_fns = [load_location]


# Define forward problem.
problem = LinearElasticity(mesh, vec=3, dim=3, ele_type=ele_type, dirichlet_bc_info=dirichlet_bc_info, location_fns=location_fns)


# Apply the automatic differentiation wrapper. 
# The flag 'use_petsc' specifies how the forward problem (could be linear or nonlinear) 
# and the backward adjoint problem (always linear) should be solved. This is a critical 
# step that makes the problem solver differentiable.
fwd_pred = ad_wrapper(problem, linear=True, use_petsc=True)


# Define the objective function 'J_total(theta)'. 
# In the following, 'sol = fwd_pred(params)' basically says U = U(theta).
def J_total(params):
    # J(u(theta), theta)   
    sol_list = fwd_pred(params)
    compliance = problem.compute_compliance(sol_list[0])
    return compliance


# Output solution files to local disk
outputs = []
def output_sol(params, obj_val):
    print(f"\nOutput solution - need to solve the forward problem again...")
    sol_list = fwd_pred(params)
    sol = sol_list[0]
    vtu_path = os.path.join(data_path, f'vtk/sol_{output_sol.counter:03d}.vtu')
    save_sol(problem.fe, np.hstack((sol, np.zeros((len(sol), 1)))), vtu_path, cell_infos=[('theta', problem.full_params[:, 0])])
    print(f"compliance = {obj_val}")
    outputs.append(obj_val)
    output_sol.counter += 1
output_sol.counter = 0


# Prepare J_total and dJ/d(theta) that are required by the MMA optimizer.
def objectiveHandle(rho):
    # MMA solver requires (J, dJ) as inputs
    # J has shape ()
    # dJ has shape (...) = rho.shape
    J, dJ = jax.value_and_grad(J_total)(rho)
    output_sol(rho, J)
    return J, dJ


# Prepare g and dg/d(theta) that are required by the MMA optimizer.
def consHandle(rho, epoch):
    # MMA solver requires (c, dc) as inputs
    # c should have shape (numConstraints,)
    # dc should have shape (numConstraints, ...)
    def computeGlobalVolumeConstraint(rho):
        g = np.mean(rho)/vf - 1.
        return g
    c, gradc = jax.value_and_grad(computeGlobalVolumeConstraint)(rho)
    c, gradc = c.reshape((1,)), gradc[None, ...]
    return c, gradc


# Finalize the details of the MMA optimizer, and solve the TO problem.
vf = 0.5
optimizationParams = {'maxIters':51, 'movelimit':0.1}
rho_ini = vf*np.ones((len(problem.fe.flex_inds), 1))
numConstraints = 1
optimize(problem.fe, rho_ini, optimizationParams, objectiveHandle, consHandle, numConstraints)
print(f"As a reminder, compliance = {J_total(np.ones((len(problem.fe.flex_inds), 1)))} for full material")


# Plot the optimization results.
obj = onp.array(outputs)
plt.figure(figsize=(10, 8))
plt.plot(onp.arange(len(obj)) + 1, obj, linestyle='-', linewidth=2, color='black')
plt.xlabel(r"Optimization step", fontsize=20)
plt.ylabel(r"Objective value", fontsize=20)
plt.tick_params(labelsize=20)
plt.tick_params(labelsize=20)
plt.show()