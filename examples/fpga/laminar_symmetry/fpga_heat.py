# SPDX-FileCopyrightText: Copyright (c) 2023 - 2024 NVIDIA CORPORATION & AFFILIATES.
# SPDX-FileCopyrightText: All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from fpga_geometry import *

import os
import warnings

import csv
import torch
import numpy as np
from sympy import Symbol, Eq, Abs, tanh, And, Or

import physicsnemo.sym
from physicsnemo.sym.hydra import to_absolute_path, instantiate_arch, PhysicsNeMoConfig
from physicsnemo.sym.utils.io import csv_to_dict
from physicsnemo.sym.solver import Solver
from physicsnemo.sym.domain import Domain
from physicsnemo.sym.geometry.primitives_3d import Box, Channel, Plane
from physicsnemo.sym.models.fourier_net import FourierNetArch
from physicsnemo.sym.domain.constraint import (
    PointwiseBoundaryConstraint,
    PointwiseInteriorConstraint,
    IntegralBoundaryConstraint,
)
from physicsnemo.sym.domain.validator import PointwiseValidator
from physicsnemo.sym.domain.inferencer import PointwiseInferencer
from physicsnemo.sym.key import Key
from physicsnemo.sym.node import Node
from physicsnemo.sym.eq.pdes.navier_stokes import NavierStokes
from physicsnemo.sym.eq.pdes.basic import NormalDotVec, GradNormal
from physicsnemo.sym.eq.pdes.diffusion import Diffusion, DiffusionInterface
from physicsnemo.sym.eq.pdes.advection_diffusion import AdvectionDiffusion


@physicsnemo.sym.main(config_path="conf_heat", config_name="config")
def run(cfg: PhysicsNeMoConfig) -> None:
    # params for simulation
    # fluid params
    nu = 0.02
    rho = 1
    # heat params
    k_fluid = 1.0
    k_solid = 5.0
    D_solid = 0.10
    D_fluid = 0.02
    source_grad = 1.5
    source_area = source_dim[0] * source_dim[2]

    # make list of nodes to unroll graph on
    ad = AdvectionDiffusion(T="theta_f", rho=rho, D=D_fluid, dim=3, time=False)
    dif = Diffusion(T="theta_s", D=D_solid, dim=3, time=False)
    dif_inteface = DiffusionInterface(
        "theta_f", "theta_s", k_fluid, k_solid, dim=3, time=False
    )
    f_grad = GradNormal("theta_f", dim=3, time=False)
    s_grad = GradNormal("theta_s", dim=3, time=False)

    # determine inputs outputs of the network
    input_keys = [Key("x"), Key("y"), Key("z")]
    output_keys = [Key("u"), Key("v"), Key("w"), Key("p")]

    # select the network and the specific configs
    if cfg.custom.arch == "FourierNetArch":
        flow_net = FourierNetArch(input_keys=input_keys, output_keys=output_keys)
        thermal_f_net = FourierNetArch(
            input_keys=input_keys, output_keys=[Key("theta_f")]
        )
        thermal_s_net = FourierNetArch(
            input_keys=input_keys, output_keys=[Key("theta_s")]
        )
    else:
        sys.exit(
            "Network not configured for this script. Please include the network in the script"
        )

    thermal_nodes = (
        ad.make_nodes()
        + dif.make_nodes()
        + dif_inteface.make_nodes()
        + f_grad.make_nodes()
        + s_grad.make_nodes()
        + [flow_net.make_node(name="flow_network", optimize=False)]
        + [thermal_f_net.make_node(name="thermal_f_network")]
        + [thermal_s_net.make_node(name="thermal_s_network")]
    )

    # make flow domain
    thermal_domain = Domain()

    # inlet
    constraint_inlet = PointwiseBoundaryConstraint(
        nodes=thermal_nodes,
        geometry=inlet,
        outvar={"theta_f": 0},
        batch_size=cfg.batch_size.inlet,
        criteria=Eq(x, channel_origin[0]),
    )
    thermal_domain.add_constraint(constraint_inlet, "inlet")

    # outlet
    constraint_outlet = PointwiseBoundaryConstraint(
        nodes=thermal_nodes,
        geometry=outlet,
        outvar={"normal_gradient_theta_f": 0},
        batch_size=cfg.batch_size.outlet,
        criteria=Eq(x, channel_origin[0] + channel_dim[0]),
    )
    thermal_domain.add_constraint(constraint_outlet, "outlet")

    # channel walls insulating
    def channel_walls_criteria(invar, params):
        sdf = fpga.sdf(invar, params)
        return np.logical_and(
            np.less(sdf["sdf"], -1e-5),
            np.less(invar["z"], channel_origin[2] + channel_dim[2] / 2.0),
        )

    channel_walls = PointwiseBoundaryConstraint(
        nodes=thermal_nodes,
        geometry=channel,
        outvar={"normal_gradient_theta_f": 0},
        batch_size=cfg.batch_size.channel_walls,
        criteria=channel_walls_criteria,
    )
    thermal_domain.add_constraint(channel_walls, "channel_walls")

    # symmetry bc
    symmetry_fluid = PointwiseBoundaryConstraint(
        nodes=thermal_nodes,
        geometry=geo,
        outvar={"theta_f__z": 0},
        batch_size=cfg.batch_size.symmetry_fluid,
        criteria=Eq(z, channel_origin[2] + channel_dim[2] / 2.0),
    )
    thermal_domain.add_constraint(symmetry_fluid, "symmetry_channel_fluid")

    symmetry_solid = PointwiseBoundaryConstraint(
        nodes=thermal_nodes,
        geometry=fpga,
        outvar={"theta_s__z": 0},
        batch_size=cfg.batch_size.symmetry_solid,
        criteria=Eq(z, channel_origin[2] + channel_dim[2] / 2.0),
    )
    thermal_domain.add_constraint(symmetry_solid, "symmetry_channel_solid")

    # fluid solid interface
    def fpga_criteria(invar, params):
        sdf = channel.sdf(invar, params)
        return np.greater(sdf["sdf"], 0)

    fluid_solid_interface = PointwiseBoundaryConstraint(
        nodes=thermal_nodes,
        geometry=fpga,
        outvar={
            "diffusion_interface_dirichlet_theta_f_theta_s": 0,
            "diffusion_interface_neumann_theta_f_theta_s": 0,
        },
        batch_size=cfg.batch_size.fluid_solid_interface,
        criteria=fpga_criteria,
    )
    thermal_domain.add_constraint(fluid_solid_interface, "fluid_solid_interface")

    # heat source
    sharpen_tanh = 60.0
    source_func_xl = (tanh(sharpen_tanh * (x - source_origin[0])) + 1.0) / 2.0
    source_func_xh = (
        tanh(sharpen_tanh * ((source_origin[0] + source_dim[0]) - x)) + 1.0
    ) / 2.0
    source_func_zl = (tanh(sharpen_tanh * (z - source_origin[2])) + 1.0) / 2.0
    source_func_zh = (
        tanh(sharpen_tanh * ((source_origin[2] + source_dim[2]) - z)) + 1.0
    ) / 2.0
    gradient_normal = (
        source_grad * source_func_xl * source_func_xh * source_func_zl * source_func_zh
    )
    heat_source = PointwiseBoundaryConstraint(
        nodes=thermal_nodes,
        geometry=fpga,
        outvar={"normal_gradient_theta_s": gradient_normal},
        batch_size=cfg.batch_size.heat_source,
        criteria=(Eq(y, source_origin[1]))
        & (z < channel_origin[2] + channel_dim[2] / 2.0),
    )
    thermal_domain.add_constraint(heat_source, "heat_source")

    # flow interior low res away from fpga
    lr_flow_interior = PointwiseInteriorConstraint(
        nodes=thermal_nodes,
        geometry=geo,
        outvar={"advection_diffusion_theta_f": 0},
        batch_size=cfg.batch_size.lr_flow_interior,
        criteria=Or(x < flow_box_origin[0], x > (flow_box_origin[0] + flow_box_dim[0])),
    )
    thermal_domain.add_constraint(lr_flow_interior, "lr_flow_interior")

    # flow interiror high res near fpga
    hr_flow_interior = PointwiseInteriorConstraint(
        nodes=thermal_nodes,
        geometry=geo,
        outvar={"advection_diffusion_theta_f": 0},
        batch_size=cfg.batch_size.hr_flow_interior,
        criteria=And(
            x > flow_box_origin[0], x < (flow_box_origin[0] + flow_box_dim[0])
        ),
    )
    thermal_domain.add_constraint(hr_flow_interior, "hr_flow_interior")

    # solid interior
    solid_interior = PointwiseInteriorConstraint(
        nodes=thermal_nodes,
        geometry=fpga,
        outvar={"diffusion_theta_s": 0},
        batch_size=cfg.batch_size.solid_interior,
        lambda_weighting={"diffusion_theta_s": 100},
    )
    thermal_domain.add_constraint(solid_interior, "solid_interior")

    # flow validation data
    file_path = "../openfoam/fpga_heat_fluid0.csv"
    if os.path.exists(to_absolute_path(file_path)):
        mapping = {
            "Points:0": "x",
            "Points:1": "y",
            "Points:2": "z",
            "U:0": "u",
            "U:1": "v",
            "U:2": "w",
            "p_rgh": "p",
            "T": "theta_f",
        }
        filename = to_absolute_path(file_path)
        values = np.loadtxt(filename, skiprows=1, delimiter=",", unpack=False)
        values = values[
            values[:, -1] + channel_origin[2] <= 0.0, :
        ]  # remove redundant data due to symmetry
        # get column keys
        csvfile = open(filename)
        reader = csv.reader(csvfile)
        first_line = next(iter(reader))

        # set dictionary
        csv_dict = {}
        for i, name in enumerate(first_line):
            if mapping is not None:
                if name.strip() in mapping.keys():
                    csv_dict[mapping[name.strip()]] = values[:, i : i + 1]
            else:
                csv_dict[name.strip()] = values[:, i : i + 1]
        openfoam_var = csv_dict

        openfoam_var["theta_f"] = (
            openfoam_var["theta_f"] / 273.15 - 1.0
        )  # normalize heat
        openfoam_var["x"] = openfoam_var["x"] + channel_origin[0]
        openfoam_var["y"] = openfoam_var["y"] + channel_origin[1]
        openfoam_var["z"] = openfoam_var["z"] + channel_origin[2]
        openfoam_invar_numpy = {
            key: value for key, value in openfoam_var.items() if key in ["x", "y", "z"]
        }
        openfoam_flow_outvar_numpy = {
            key: value
            for key, value in openfoam_var.items()
            if key in ["u", "v", "w", "p"]
        }
        openfoam_thermal_outvar_numpy = {
            key: value
            for key, value in openfoam_var.items()
            if key in ["u", "v", "w", "p", "theta_f"]
        }
        openfoam_flow_validator = PointwiseValidator(
            nodes=thermal_nodes,
            invar=openfoam_invar_numpy,
            true_outvar=openfoam_thermal_outvar_numpy,
        )
        thermal_domain.add_validator(
            openfoam_flow_validator,
            "thermal_flow_data",
        )
    else:
        warnings.warn(
            f"Directory {file_path} does not exist. Will skip adding validators. Please download the additional files from NGC https://catalog.ngc.nvidia.com/orgs/nvidia/teams/physicsnemo/resources/physicsnemo_sym_examples_supplemental_materials"
        )

    # solid data
    file_path = "../openfoam/fpga_heat_solid0.csv"
    if os.path.exists(to_absolute_path(file_path)):
        mapping = {"Points:0": "x", "Points:1": "y", "Points:2": "z", "T": "theta_s"}
        filename = to_absolute_path(file_path)
        values = np.loadtxt(filename, skiprows=1, delimiter=",", unpack=False)
        values = values[
            values[:, -1] + channel_origin[2] <= 0.0, :
        ]  # remove redundant data due to symmetry
        # get column keys
        csvfile = open(filename)
        reader = csv.reader(csvfile)
        first_line = next(iter(reader))

        # set dictionary
        csv_dict = {}
        for i, name in enumerate(first_line):
            if mapping is not None:
                if name.strip() in mapping.keys():
                    csv_dict[mapping[name.strip()]] = values[:, i : i + 1]
            else:
                csv_dict[name.strip()] = values[:, i : i + 1]
        openfoam_var = csv_dict

        openfoam_var["theta_s"] = (
            openfoam_var["theta_s"] / 273.15 - 1.0
        )  # normalize heat
        openfoam_var["x"] = openfoam_var["x"] + channel_origin[0]
        openfoam_var["y"] = openfoam_var["y"] + channel_origin[1]
        openfoam_var["z"] = openfoam_var["z"] + channel_origin[2]
        openfoam_invar_solid_numpy = {
            key: value for key, value in openfoam_var.items() if key in ["x", "y", "z"]
        }
        openfoam_outvar_solid_numpy = {
            key: value for key, value in openfoam_var.items() if key in ["theta_s"]
        }
        openfoam_solid_validator = PointwiseValidator(
            nodes=thermal_nodes,
            invar=openfoam_invar_solid_numpy,
            true_outvar=openfoam_outvar_solid_numpy,
        )
        thermal_domain.add_validator(
            openfoam_solid_validator,
            "thermal_solid_data",
        )
    else:
        warnings.warn(
            f"Directory {file_path} does not exist. Will skip adding validators. Please download the additional files from NGC https://catalog.ngc.nvidia.com/orgs/nvidia/teams/physicsnemo/resources/physicsnemo_sym_examples_supplemental_materials"
        )

    # make solver
    thermal_slv = Solver(cfg, thermal_domain)

    # start thermal solver
    thermal_slv.solve()


if __name__ == "__main__":
    run()
