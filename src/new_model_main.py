import argparse
import gzip
import itertools as it
import multiprocessing as mp
import os
import pickle
from functools import partial

import matplotlib.pyplot as plt
import numpy as np
import scipy.interpolate
import scipy.optimize
import scipy.signal
import ternary
import tqdm
from scipy.special import comb as scipy_comb
from ternary.helpers import simplex_iterator

from src import model_parameter_functions, config

constant_set = config.Constants()
color_set = config.Color()


def natural_dist(_c13_ratio, carbon_num):
    c12_ratio = 1 - _c13_ratio
    total_num = carbon_num + 1
    output = []
    for index in range(total_num):
        output.append(
            scipy_comb(carbon_num, index) * _c13_ratio ** index * c12_ratio ** (carbon_num - index))
    return np.array(output)


def split_equal_dist(source_mid, target_carbon_num):
    carbon_num = len(source_mid) - 1
    if carbon_num % 2 != 0:
        raise ValueError("Length is not multiply of 2 !!!")
    new_carbon_num = target_carbon_num
    final_output_vector = np.zeros(new_carbon_num + 1)
    final_output_vector[0] = source_mid[0]
    final_output_vector[-1] = source_mid[-1]
    average_ratio = (1 - final_output_vector[0] - final_output_vector[-1]) / (new_carbon_num - 1)
    for i in range(1, new_carbon_num):
        final_output_vector[i] = average_ratio

    # _c12_ratio = np.power(source_mid[0], (1 / carbon_num))
    # _c13_ratio = 1 - _c12_ratio
    #
    # final_output_vector = natural_dist(_c13_ratio, target_carbon_num)
    return final_output_vector


def collect_all_data(
        data_dict, _metabolite_name, _label_list, _tissue, _mouse_id_list=None, convolve=False,
        split=0, mean=True):
    matrix = []
    for label in _label_list:
        if _mouse_id_list is None:
            _mouse_id_list = data_dict[label].keys()
        for mouse_label in _mouse_id_list:
            data_for_mouse = data_dict[label][mouse_label]
            data_vector = data_for_mouse[_tissue][_metabolite_name]
            if convolve:
                data_vector = np.convolve(data_vector, data_vector)
            elif split != 0:
                data_vector = split_equal_dist(data_vector, split)
            matrix.append(data_vector)
    result_matrix = np.array(matrix).transpose()
    if mean:
        return result_matrix.mean(axis=1)
    else:
        return result_matrix.transpose().reshape([-1])


def flux_balance_constraint_constructor(balance_list, complete_flux_dict):
    flux_balance_multiply_array_list = []
    flux_balance_constant_vector_list = []
    for balance_dict in balance_list:
        new_balance_array = np.zeros(len(complete_flux_dict))
        flux_name_list = balance_dict['input'] + balance_dict['output']
        value_list = [-1 for _ in balance_dict['input']] + [1 for _ in balance_dict['output']]
        for flux_name, value in zip(flux_name_list, value_list):
            flux_index = complete_flux_dict[flux_name]
            new_balance_array[flux_index] = value
        flux_balance_multiply_array_list.append(new_balance_array)
        flux_balance_constant_vector_list.append(0)
    flux_balance_matrix = np.array(flux_balance_multiply_array_list)
    flux_balance_constant_vector = np.array(flux_balance_constant_vector_list)
    return flux_balance_matrix, flux_balance_constant_vector


def mid_constraint_constructor(mid_constraint_list, complete_flux_dict):
    complete_var_num = len(complete_flux_dict)
    substrate_mid_matrix_list = []
    flux_sum_matrix_list = []
    target_mid_vector_list = []
    for mid_constraint_dict in mid_constraint_list:
        target_mid_vector = mid_constraint_dict[constant_set.target_label]
        vector_dim = len(target_mid_vector)
        new_substrate_mid_matrix_list = [np.zeros(complete_var_num) for _ in range(vector_dim)]
        new_flux_sum_matrix_list = [np.zeros(complete_var_num) for _ in range(vector_dim)]
        target_mid_vector_list.append(target_mid_vector)
        for flux_name, vector in mid_constraint_dict.items():
            if flux_name == constant_set.target_label:
                continue
            flux_index = complete_flux_dict[flux_name]
            for index, vector_value in enumerate(vector):
                new_substrate_mid_matrix_list[index][flux_index] = vector_value
                new_flux_sum_matrix_list[index][flux_index] = 1
        substrate_mid_matrix_list.extend(new_substrate_mid_matrix_list)
        flux_sum_matrix_list.extend(new_flux_sum_matrix_list)
    substrate_mid_matrix = np.array(substrate_mid_matrix_list)
    flux_sum_matrix = np.array(flux_sum_matrix_list)
    target_mid_vector = np.hstack(target_mid_vector_list) + constant_set.eps_for_log
    optimal_obj_value = -np.sum(target_mid_vector * np.log(target_mid_vector))
    return substrate_mid_matrix, flux_sum_matrix, target_mid_vector, optimal_obj_value


def constant_flux_constraint_constructor(constant_flux_dict, complete_flux_dict):
    constant_flux_multiply_array_list = []
    constant_flux_constant_vector_list = []
    for constant_flux, value in constant_flux_dict.items():
        new_balance_array = np.zeros(len(complete_flux_dict))
        flux_index = complete_flux_dict[constant_flux]
        new_balance_array[flux_index] = 1
        constant_flux_multiply_array_list.append(new_balance_array)
        constant_flux_constant_vector_list.append(-value)
    constant_flux_matrix = np.array(constant_flux_multiply_array_list)
    constant_constant_vector = np.array(constant_flux_constant_vector_list)
    return constant_flux_matrix, constant_constant_vector


def cross_entropy_obj_func_constructor(substrate_mid_matrix, flux_sum_matrix, target_mid_vector):
    def cross_entropy_objective_func(complete_vector):
        # complete_vector = np.hstack([f_vector, constant_flux_array]).reshape([-1, 1])
        complete_vector = complete_vector.reshape([-1, 1])
        predicted_mid_vector = (
                substrate_mid_matrix @ complete_vector / (flux_sum_matrix @ complete_vector) + constant_set.eps_for_log)
        cross_entropy = -target_mid_vector.reshape([1, -1]) @ np.log(predicted_mid_vector)
        return cross_entropy

    return cross_entropy_objective_func


def cross_entropy_jacobi_func_constructor(substrate_mid_matrix, flux_sum_matrix, target_mid_vector):
    def cross_entropy_jacobi_func(complete_vector):
        complete_vector = complete_vector.reshape([-1, 1])
        substrate_mid_part = substrate_mid_matrix / (substrate_mid_matrix @ complete_vector)
        flux_sum_part = flux_sum_matrix / (flux_sum_matrix @ complete_vector)
        jacobian_vector = target_mid_vector.reshape([1, -1]) @ (flux_sum_part - substrate_mid_part)
        return jacobian_vector.reshape([-1])

    return cross_entropy_jacobi_func


def eq_func_constructor(complete_balance_matrix, complete_balance_vector):
    def eq_func(complete_vector):
        result = complete_balance_matrix @ complete_vector.reshape([-1, 1]) + complete_balance_vector.reshape([-1, 1])
        return result.reshape([-1])

    return eq_func


def eq_func_jacob_constructor(complete_balance_matrix, complete_balance_vector):
    def eq_func_jacob(complete_vector):
        return complete_balance_matrix

    return eq_func_jacob


def start_point_generator(
        complete_balance_matrix, complete_balance_vector, bounds, maximal_failed_time=10):
    a_eq = complete_balance_matrix
    b_eq = -complete_balance_vector
    raw_lb, raw_ub = bounds
    result = None
    failed_time = 0
    num_variable = a_eq.shape[1]
    while failed_time < maximal_failed_time:
        random_obj = np.random.random(num_variable) - 0.4
        lp_lb = raw_lb + np.random.random(num_variable) * 4 + 1
        lp_ub = raw_ub * (np.random.random(num_variable) * 0.2 + 0.8)
        bounds_matrix = np.vstack([lp_lb, lp_ub]).T
        try:
            res = scipy.optimize.linprog(
                random_obj, A_eq=a_eq, b_eq=b_eq, bounds=bounds_matrix, method="simplex",
                options={'tol': 1e-10})  # "disp": True
        except ValueError:
            failed_time += 1
            continue
        else:
            if res.success:
                result = np.array(res.x)
                break
            failed_time += 1
    return result


def one_case_solver_slsqp(
        flux_balance_matrix, flux_balance_constant_vector, substrate_mid_matrix, flux_sum_matrix, target_mid_vector,
        optimal_obj_value, complete_flux_dict, constant_flux_dict, bounds,
        optimization_repeat_time, label=None, fitted=True, **other_parameters):
    constant_flux_matrix, constant_constant_vector = constant_flux_constraint_constructor(
        constant_flux_dict, complete_flux_dict)
    complete_balance_matrix = np.vstack(
        [flux_balance_matrix, constant_flux_matrix])
    complete_balance_vector = np.hstack(
        [flux_balance_constant_vector, constant_constant_vector])
    cross_entropy_objective_func = cross_entropy_obj_func_constructor(
        substrate_mid_matrix, flux_sum_matrix, target_mid_vector)
    cross_entropy_jacobi_func = cross_entropy_jacobi_func_constructor(
        substrate_mid_matrix, flux_sum_matrix, target_mid_vector)
    eq_func = eq_func_constructor(complete_balance_matrix, complete_balance_vector)
    eq_func_jacob = eq_func_jacob_constructor(complete_balance_matrix, complete_balance_vector)

    eq_cons = {'type': 'eq', 'fun': eq_func, 'jac': eq_func_jacob}
    bound_object = scipy.optimize.Bounds(*bounds)
    start_vector = start_point_generator(
        complete_balance_matrix, complete_balance_vector, bounds)
    # gradient_validation(cross_entropy_objective_func, cross_entropy_jacobi, start_vector)
    if start_vector is None:
        result_dict = {}
        obj_value = 999999
        success = False
    else:
        if not fitted:
            obj_value = cross_entropy_objective_func(start_vector)[0][0]
            success = True
            result_dict = {
                flux_name: flux_value for flux_name, flux_value
                in zip(complete_flux_dict.keys(), start_vector)}
        else:
            result_dict = {}
            success = False
            obj_value = 999999
            for _ in range(optimization_repeat_time):
                start_vector = start_point_generator(
                    complete_balance_matrix, complete_balance_vector, bounds)
                if start_vector is None:
                    continue
                current_result = scipy.optimize.minimize(
                    cross_entropy_objective_func, start_vector, method='SLSQP', jac=cross_entropy_jacobi_func,
                    constraints=[eq_cons], options={'ftol': 1e-9, 'maxiter': 500}, bounds=bound_object)  # 'disp': True,
                if current_result.success and current_result.fun < obj_value:
                    result_dict = {
                        flux_name: flux_value for flux_name, flux_value
                        in zip(complete_flux_dict.keys(), current_result.x)}
                    obj_value = current_result.fun
                    success = current_result.success
    return config.Result(result_dict, obj_value, success, optimal_obj_value, label)


def calculate_one_tissue_tca_contribution(input_net_flux_list):
    real_flux_list = []
    total_input_flux = 0
    total_output_flux = 0
    for net_flux in input_net_flux_list:
        if net_flux > 0:
            total_input_flux += net_flux
        else:
            total_output_flux -= net_flux
    for net_flux in input_net_flux_list:
        current_real_flux = 0
        if net_flux > 0:
            current_real_flux = net_flux - net_flux / total_input_flux * total_output_flux
        real_flux_list.append(current_real_flux)
    real_flux_array = np.array(real_flux_list)
    return real_flux_array


def one_time_prediction(predicted_vector_dim, mid_constraint_dict, flux_value_dict):
    predicted_vector = np.zeros(predicted_vector_dim)
    total_flux_value = 0
    for flux_name, mid_vector in mid_constraint_dict.items():
        if flux_name == constant_set.target_label:
            continue
        else:
            flux_value = flux_value_dict[flux_name]
            total_flux_value += flux_value
            predicted_vector += flux_value * mid_vector
    predicted_vector /= total_flux_value
    return predicted_vector


def evaluation_for_one_flux(result_dict, constant_dict, mid_constraint_list, mid_size_dict):
    flux_value_dict = dict(result_dict)
    flux_value_dict.update(constant_dict)
    predicted_mid_dict = {}
    for mid_constraint_dict in mid_constraint_list:
        name = "_".join([name for name in mid_constraint_dict.keys() if name != 'target'])
        predicted_vector = one_time_prediction(mid_size_dict[name], mid_constraint_dict, flux_value_dict)
        predicted_mid_dict[name] = predicted_vector
    return predicted_mid_dict


def plot_raw_mid_bar(data_dict, color_dict=None, error_bar_dict=None, title=None, save_path=None):
    edge = 0.2
    bar_total_width = 0.7
    group_num = len(data_dict)
    bar_unit_width = bar_total_width / group_num
    array_len = 0
    for data_name, np_array in data_dict.items():
        if array_len == 0:
            array_len = len(np_array)
        elif len(np_array) != array_len:
            raise ValueError("Length of array not equal: {}".format(data_name))
    fig_size = (array_len + edge * 2, 4)
    fig, ax = plt.subplots(figsize=fig_size)
    x_mid_loc = np.arange(array_len) + 0.5
    x_left_loc = x_mid_loc - bar_total_width / 2
    for index, (data_name, mid_array) in enumerate(data_dict.items()):
        if color_dict is not None:
            current_color = color_dict[data_name]
        else:
            current_color = None
        if error_bar_dict is not None and data_name in error_bar_dict:
            error_bar_vector = error_bar_dict[data_name]
            error_bar_param = {
                'ecolor': current_color,
                'capsize': 3,
                'elinewidth': 1.5
            }
        else:
            error_bar_vector = None
            error_bar_param = {}
        x_loc = x_left_loc + index * bar_unit_width + bar_unit_width / 2
        ax.bar(
            x_loc, mid_array, width=bar_unit_width, color=current_color,
            alpha=color_set.alpha_for_bar_plot, label=data_name, yerr=error_bar_vector, error_kw=error_bar_param)
    # ax.set_xlabel(data_dict.keys())
    ax.set_ylim([0, 1])
    ax.set_xlim([-edge, array_len + edge])
    ax.set_xticks(x_mid_loc)
    ax.set_xticklabels([])
    ax.set_yticks(np.arange(0, 1.1, 0.2))
    ax.set_yticklabels([])
    # ax.legend()
    if title:
        ax.set_title(title)
    if save_path:
        fig.savefig(save_path, dpi=fig.dpi)


# data_matrix: show the location of heatmap
def plot_heat_map(
        data_matrix, x_free_variable, y_free_variable, cmap=None, cbar_name=None, title=None, save_path=None):
    fig, ax = plt.subplots()
    im = ax.imshow(data_matrix, cmap=cmap)
    ax.set_xlim([0, x_free_variable.total_num])
    ax.set_ylim([0, y_free_variable.total_num])
    ax.set_xticks(x_free_variable.tick_in_range)
    ax.set_yticks(y_free_variable.tick_in_range)
    ax.set_xticklabels(x_free_variable.tick_labels)
    ax.set_yticklabels(y_free_variable.tick_labels)
    if title:
        ax.set_title(title)
    if cbar_name:
        cbar = ax.figure.colorbar(im, ax=ax)
        cbar.ax.set_ylabel(cbar_name, rotation=-90, va="bottom")
    if save_path:
        # print(save_path)
        fig.savefig(save_path, dpi=fig.dpi)


def plot_violin_distribution(data_dict, color_dict=None, cutoff=0.5, title=None, save_path=None):
    fig, ax = plt.subplots()
    data_list_for_violin = data_dict.values()
    tissue_label_list = data_dict.keys()
    x_axis_position = np.arange(1, len(tissue_label_list) + 1)

    parts = ax.violinplot(data_list_for_violin, showmedians=True, showextrema=True)
    if color_dict is not None:
        if isinstance(color_dict, np.ndarray):
            new_color_dict = {key: color_dict for key in data_dict}
            color_dict = new_color_dict
        color_list = [color_dict[key] for key in tissue_label_list]
        parts['cmaxes'].set_edgecolor(color_list)
        parts['cmins'].set_edgecolor(color_list)
        parts['cbars'].set_edgecolor(color_list)
        parts['cmedians'].set_edgecolor(color_set.orange)
        for pc, color in zip(parts['bodies'], color_list):
            pc.set_facecolor(color)
            pc.set_alpha(color_set.alpha_value)
    if cutoff is not None:
        ax.axhline(cutoff, linestyle='--', color=color_set.orange)
    ax.set_ylim([-0.1, 1.1])
    ax.set_xticks(x_axis_position)
    ax.set_xticklabels(tissue_label_list)
    if title:
        ax.set_title(title)
    if save_path:
        # print(save_path)
        fig.savefig(save_path, dpi=fig.dpi)


def plot_box_distribution(data_dict, save_path=None, title=None, broken_yaxis=None):
    def color_edges(box_parts):
        for part_name, part_list in box_parts.items():
            if part_name == 'medians':
                current_color = color_set.orange
            else:
                current_color = color_set.blue
            for part in part_list:
                part.set_color(current_color)

    data_list_for_box = data_dict.values()
    tissue_label_list = data_dict.keys()
    x_axis_position = np.arange(1, len(tissue_label_list) + 1)

    if broken_yaxis is None:
        fig, ax = plt.subplots()
        parts = ax.boxplot(data_list_for_box, whis='range')
        color_edges(parts)
        ax.set_xticks(x_axis_position)
        ax.set_xticklabels(tissue_label_list)
        if title:
            ax.set_title(title)
    else:
        fig, (ax1, ax2) = plt.subplots(2, 1, sharex=True)
        parts1 = ax1.boxplot(data_list_for_box, whis='range')
        parts2 = ax2.boxplot(data_list_for_box, whis='range')
        color_edges(parts1)
        color_edges(parts2)
        ax1.set_ylim([broken_yaxis[1], None])
        ax2.set_ylim([-50, broken_yaxis[0]])
        ax1.spines['bottom'].set_visible(False)
        ax2.spines['top'].set_visible(False)
        ax1.xaxis.tick_top()
        ax2.set_xticks(x_axis_position)
        ax2.set_xticklabels(tissue_label_list)

    if save_path:
        fig.savefig(save_path, dpi=fig.dpi)


# Plot a scatter in triangle based on data_matrix
# data_matrix: N-3 matrix. Each row is a point with 3 coordinate
def plot_ternary_scatter(data_matrix):
    ### Scatter Plot
    scale = 1
    figure, tax = ternary.figure(scale=scale)
    tax.set_title("Scatter Plot", fontsize=20)
    tax.boundary(linewidth=2.0)
    tax.gridlines(multiple=0.1, color="blue")
    # Plot a few different styles with a legend
    # points = [data_matrix]
    # tax.heatmap()
    tax.scatter(data_matrix, marker='s', color='red', label="Red Squares")
    tax.legend()
    tax.ticks(axis='lbr', linewidth=1, multiple=0.1)


# Each row of data matrix is a point in triple tuple
# In cartesian cor, the left bottom corner of triangle is the origin.
# The scale of all triangle points is 1.
# Order of ternary cor: x1: bottom (to right) x2: right (to left) x3: left (to bottom)
def plot_ternary_density(
        tri_data_matrix, sigma: float = 1, bin_num: int = 2 ** 8, mean=False, title=None, save_path=None):
    sqrt_3 = np.sqrt(3)

    def standard_2dnormal(x, y, _sigma):
        return np.exp(-0.5 / _sigma ** 2 * (x ** 2 + y ** 2)) / (2 * np.pi * _sigma ** 2)

    # Each row is the cartesian cor.
    def tri_to_car(input_data_matrix):
        y_value = input_data_matrix[:, 1] * sqrt_3 / 2
        x_value = input_data_matrix[:, 0] + y_value / sqrt_3
        return np.vstack([x_value, y_value]).T

    def car_to_tri(input_data_matrix):
        y_value = input_data_matrix[:, 1]
        x2_value = y_value / (sqrt_3 / 2)
        x1_value = input_data_matrix[:, 0] - y_value / sqrt_3
        return np.vstack([x1_value, x2_value]).T

    def gaussian_kernel_generator(_bin_num, _sigma):
        x = np.linspace(0, 1, _bin_num) - 0.5
        y = np.linspace(0, 1, _bin_num) - 0.5
        X, Y = np.meshgrid(x, y)
        gaussian_kernel = standard_2dnormal(X, Y, _sigma)
        return np.rot90(gaussian_kernel)

    def bin_car_data_points(_car_data_matrix, _bin_num):
        histogram, _, _ = np.histogram2d(
            _car_data_matrix[:, 0], _car_data_matrix[:, 1], bins=np.linspace(0, 1, _bin_num + 1))
        return histogram

    def complete_tri_set_interpolation(_location_list, _value_list, _scale):
        result_tri_array = np.array(list(simplex_iterator(_scale))) / _scale
        result_car_array = tri_to_car(result_tri_array)
        result_value_array = scipy.interpolate.griddata(
            np.array(_location_list), np.array(_value_list), result_car_array, method='cubic')
        target_dict = {}
        for (i, j, k), result_value in zip(simplex_iterator(bin_num), result_value_array):
            target_dict[(i, j)] = result_value
        return target_dict

    car_data_matrix = tri_to_car(tri_data_matrix)
    data_bin_matrix = bin_car_data_points(car_data_matrix, bin_num)
    gaussian_kernel_matrix = gaussian_kernel_generator(bin_num, sigma)
    car_blurred_matrix = scipy.signal.convolve2d(data_bin_matrix, gaussian_kernel_matrix, mode='same')
    x_axis = y_axis = np.linspace(0, 1, bin_num)
    location_list = []
    value_list = []
    for x_index, x_value in enumerate(x_axis):
        for y_index, y_value in enumerate(y_axis):
            location_list.append([x_value, y_value])
            value_list.append(car_blurred_matrix[x_index, y_index])
    complete_density_dict = complete_tri_set_interpolation(location_list, value_list, bin_num)
    fig, tax = ternary.figure(scale=bin_num)
    tax.heatmap(complete_density_dict, cmap='Blues', style="h")
    tax.boundary(linewidth=1.0)
    tick_labels = list(np.linspace(0, bin_num, 6) / bin_num)
    tax.ticks(axis='lbr', ticks=tick_labels, linewidth=1, tick_formats="")
    tax.clear_matplotlib_ticks()
    tax.set_title(title)
    plt.tight_layout()
    if mean:
        mean_value = tri_data_matrix.mean(axis=0).reshape([1, -1]) * bin_num
        tax.scatter(mean_value, marker='o', color=color_set.orange, zorder=100)
    if save_path:
        print(save_path)
        fig.savefig(save_path, dpi=fig.dpi)
    # tax.show()


def parallel_solver_single(
        var_parameter_dict, const_parameter_dict, one_case_solver_func, hook_in_each_iteration):
    result = one_case_solver_func(**const_parameter_dict, **var_parameter_dict)
    # hook_result = hook_in_each_iteration(result, **const_parameter_dict, **var_parameter_dict)
    hook_result = result_processing_each_iteration_template(
        result, contribution_func=hook_in_each_iteration)
    return result, hook_result


def parallel_solver(
        data_loader_func, parameter_construction_func,
        one_case_solver_func, hook_in_each_iteration, model_name,
        hook_after_all_iterations, parallel_num, **other_parameters):
    # manager = multiprocessing.Manager()
    # q = manager.Queue()
    # result = pool.map_async(task, [(x, q) for x in range(10)])
    debug = False

    if parallel_num is None:
        cpu_count = os.cpu_count()
        if cpu_count < 10:
            parallel_num = cpu_count - 1
        else:
            parallel_num = min(cpu_count, 16)
    if parallel_num < 8:
        chunk_size = 40
    else:
        chunk_size = 80

    model_mid_data_dict = data_loader_func(**other_parameters)
    const_parameter_dict, var_parameter_list = parameter_construction_func(
        model_mid_data_dict=model_mid_data_dict, parallel_num=parallel_num, model_name=model_name,
        **other_parameters)

    if not isinstance(var_parameter_list, list):
        var_parameter_list, var_parameter_list2 = it.tee(var_parameter_list)
        total_length = const_parameter_dict['iter_length']
    else:
        var_parameter_list2 = var_parameter_list
        total_length = len(var_parameter_list)

    if debug:
        result_list = []
        hook_result_list = []
        for var_parameter_dict in var_parameter_list:
            result, hook_result = parallel_solver_single(
                var_parameter_dict, const_parameter_dict, one_case_solver_func, hook_in_each_iteration)
            result_list.append(result)
            hook_result_list.append(hook_result)
    else:
        with mp.Pool(processes=parallel_num) as pool:
            raw_result_iter = pool.imap(
                partial(
                    parallel_solver_single, const_parameter_dict=const_parameter_dict,
                    one_case_solver_func=one_case_solver_func,
                    hook_in_each_iteration=hook_in_each_iteration),
                var_parameter_list, chunk_size)
            raw_result_list = list(tqdm.tqdm(
                raw_result_iter, total=total_length, smoothing=0, maxinterval=5,
                desc="Computation progress of {}".format(model_name)))

        result_iter, hook_result_iter = zip(*raw_result_list)
        result_list = list(result_iter)
        hook_result_list = list(hook_result_iter)
    if not os.path.isdir(constant_set.output_direct):
        os.mkdir(constant_set.output_direct)
    hook_after_all_iterations(result_list, hook_result_list, const_parameter_dict, var_parameter_list2)


#     output_data_dict = {
#         'result_list': result_list,
#         'processed_result_list': processed_result_list,
#         ...
#     }
#     self.result_dict = result_dict
#     self.obj_value = obj_value
#     self.success = success
#     self.minimal_obj_value = minimal_obj_value
def fitting_result_display(
        data_loader_func, model_name, model_construction_func, obj_tolerance,
        **other_parameters):
    server_data = False
    total_output_direct = constant_set.output_direct
    model_mid_data_dict = data_loader_func(**other_parameters)

    balance_list, mid_constraint_list = model_construction_func(model_mid_data_dict)
    experimental_label = 'Experimental MID'
    predicted_label = 'Calculated MID'
    plot_color_dict = {experimental_label: color_set.blue, predicted_label: color_set.orange}

    target_vector_dict = {}
    mid_size_dict = {}
    for mid_constraint_dict in mid_constraint_list:
        target_vector = mid_constraint_dict[constant_set.target_label]
        name = "_".join([name for name in mid_constraint_dict.keys() if name != 'target'])
        target_vector_dict[name] = target_vector
        mid_size_dict[name] = len(target_vector)

    if server_data:
        output_direct = "{}/{}_server".format(total_output_direct, model_name)
    else:
        output_direct = "{}/{}".format(total_output_direct, model_name)
    raw_data_dict_gz_file = "{}/raw_output_data_dict.gz".format(output_direct)
    with gzip.open(raw_data_dict_gz_file, 'rb') as f_in:
        raw_input_data_dict = pickle.load(f_in)
    result_list: list = raw_input_data_dict['result_list']
    predicted_mid_collection_dict = {}
    for result_object in result_list:
        if result_object.success:
            obj_diff = result_object.obj_value - result_object.minimal_obj_value
            if obj_diff < obj_tolerance:
                predicted_mid_dict = evaluation_for_one_flux(
                    result_object.result_dict, {}, mid_constraint_list, mid_size_dict)
                for mid_name, mid_vector in predicted_mid_dict.items():
                    if mid_name not in predicted_mid_collection_dict:
                        predicted_mid_collection_dict[mid_name] = []
                    predicted_mid_collection_dict[mid_name].append(mid_vector)
    for mid_name, mid_vector_list in predicted_mid_collection_dict.items():
        predicted_mid_mean = np.mean(mid_vector_list, axis=0)
        predicted_mid_std = np.std(mid_vector_list, axis=0)
        target_mid_vector = target_vector_dict[mid_name]
        plot_data_dict = {experimental_label: target_mid_vector, predicted_label: predicted_mid_mean}
        plot_errorbar_dict = {predicted_label: predicted_mid_std}
        save_path = "{}/complete_mid_prediction_distribution_{}.png".format(output_direct, mid_name)
        plot_raw_mid_bar(
            plot_data_dict, color_dict=plot_color_dict, error_bar_dict=plot_errorbar_dict,
            title=mid_name, save_path=save_path)
    plt.show()


def result_processing_each_iteration_template(result: config.Result, contribution_func):
    processed_dict = {}
    if result.success:
        processed_dict['obj_diff'] = result.obj_value - result.minimal_obj_value
        processed_dict['valid'], processed_dict['contribution_dict'] = contribution_func(
            result.result_dict)
    else:
        processed_dict['obj_diff'] = np.nan
        processed_dict['valid'], processed_dict['contribution_dict'] = contribution_func(
            result.result_dict, empty=True)
    return processed_dict


def parser_main():
    parameter_dict = {
        'model1': model_parameter_functions.model1_parameters,
        'model1_m5': model_parameter_functions.model1_m5_parameters,
        'model1_m9': model_parameter_functions.model1_m9_parameters,
        'model1_lactate': model_parameter_functions.model1_lactate_parameters,
        'model1_lactate_m4': model_parameter_functions.model1_lactate_m4_parameters,
        'model1_lactate_m10': model_parameter_functions.model1_lactate_m10_parameters,
        'model1_lactate_m11': model_parameter_functions.model1_lactate_m11_parameters,
        'model1_all': model_parameter_functions.model1_all_tissue,
        'model1_all_m5': model_parameter_functions.model1_all_tissue_m5,
        'model1_all_m9': model_parameter_functions.model1_all_tissue_m9,
        'model1_all_lactate': model_parameter_functions.model1_all_tissue_lactate,
        'model1_all_lactate_m4': model_parameter_functions.model1_all_tissue_lactate_m4,
        'model1_all_lactate_m10': model_parameter_functions.model1_all_tissue_lactate_m10,
        'model1_all_lactate_m11': model_parameter_functions.model1_all_tissue_lactate_m11,
        'model1_all_hypoxia': model_parameter_functions.model1_hypoxia_correction,
        'model1_unfitted': model_parameter_functions.model1_unfitted_parameters,
        'parameter': model_parameter_functions.model1_parameter_sensitivity,
        'model3': model_parameter_functions.model3_parameters,
        'model3_all': model_parameter_functions.model3_all_tissue,
        'model3_all_m5': model_parameter_functions.model3_all_tissue_m5,
        'model3_all_m9': model_parameter_functions.model3_all_tissue_m9,
        'model3_all_lactate': model_parameter_functions.model3_all_tissue_lactate,
        'model3_all_lactate_m4': model_parameter_functions.model3_all_tissue_lactate_m4,
        'model3_all_lactate_m10': model_parameter_functions.model3_all_tissue_lactate_m10,
        'model3_all_lactate_m11': model_parameter_functions.model3_all_tissue_lactate_m11,
        'model3_unfitted': model_parameter_functions.model3_unfitted_parameters,
        'model5': model_parameter_functions.model5_parameters,
        'model5_comb2': model_parameter_functions.model5_comb2_parameters,
        'model5_comb3': model_parameter_functions.model5_comb3_parameters,
        'model5_unfitted': model_parameter_functions.model5_unfitted_parameters,
        'model6': model_parameter_functions.model6_parameters,
        'model6_m2': model_parameter_functions.model6_m2_parameters,
        'model6_m3': model_parameter_functions.model6_m3_parameters,
        'model6_m4': model_parameter_functions.model6_m4_parameters,
        'model6_unfitted': model_parameter_functions.model6_unfitted_parameters,
        'model7': model_parameter_functions.model7_parameters,
        'model7_m2': model_parameter_functions.model7_m2_parameters,
        'model7_m3': model_parameter_functions.model7_m3_parameters,
        'model7_m4': model_parameter_functions.model7_m3_parameters,
        'model7_unfitted': model_parameter_functions.model7_unfitted_parameters}
    parser = argparse.ArgumentParser(description='MFA for multi-tissue model by Shiyu Liu.')
    parser.add_argument(
        'model_name', choices=parameter_dict.keys(), help='The name of model you want to compute.')
    parser.add_argument(
        '-t', '--test_mode', action='store_true', default=False,
        help='Whether the code is executed in test mode, which means less sample number and shorter time.')
    parser.add_argument(
        '-f', '--fitting_result', action='store_true', default=False,
        help='Whether to show the distribution of fitting result after simulating the sample.')
    parser.add_argument(
        '-p', '--parallel_num', type=int, default=None,
        help='Number of parallel processes. If not provided, it will be selected according to CPU cores.')

    args = parser.parse_args()
    current_model_parameter_dict = parameter_dict[args.model_name](args.test_mode)
    parallel_solver(
        **current_model_parameter_dict, parallel_num=args.parallel_num, one_case_solver_func=one_case_solver_slsqp)
    if args.fitting_result:
        fitting_result_display(**current_model_parameter_dict)


if __name__ == '__main__':
    parser_main()
