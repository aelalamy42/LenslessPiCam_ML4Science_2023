# #############################################################################
# benchmark_recon.py
# =========
# Authors :
# Yohann PERRON
# Eric BEZZAM [ebezzam@gmail.com]
# #############################################################################

"""
Benchmark reconstruction algorithms
==============
This script benchmarks reconstruction algorithms on the DiffuserCam test dataset.
The algorithm benchmarked and the number of iterations can be set in the config file : benchmark.yaml.
For unrolled algorithms, the results of the unrolled training (json file) are loaded from the benchmark/results folder.
"""

import hydra
from hydra.utils import get_original_cwd

import time
import numpy as np
import glob
import json
import os
import pathlib as plib
from lensless.eval.benchmark import benchmark
import matplotlib.pyplot as plt
from lensless import ADMM, FISTA, GradientDescent, NesterovGradientDescent
from lensless.utils.dataset import DiffuserCamTestDataset, DigiCamCelebA
from lensless.utils.io import save_image

import torch
from torch.utils.data import Subset


@hydra.main(version_base=None, config_path="../../configs", config_name="benchmark")
def benchmark_recon(config):

    # set seed
    torch.manual_seed(config.seed)
    np.random.seed(config.seed)
    generator = torch.Generator().manual_seed(config.seed)

    downsample = config.downsample
    n_files = config.n_files
    n_iter_range = config.n_iter_range

    # check if GPU is available
    if torch.cuda.is_available() and config.device[:4] == "cuda":
        device = config.device
    else:
        device = "cpu"

    # Benchmark dataset
    dataset = config.dataset
    if dataset == "DiffuserCam":
        benchmark_dataset = DiffuserCamTestDataset(n_files=n_files, downsample=downsample)
        psf = benchmark_dataset.psf.to(device)
        crop = None

    elif dataset == "DigiCamCelebA":

        dataset = DigiCamCelebA(
            data_dir=os.path.join(get_original_cwd(), config.files.dataset),
            celeba_root=config.files.celeba_root,
            psf_path=os.path.join(get_original_cwd(), config.files.psf),
            downsample=config.files.downsample,
            vertical_shift=config.files.vertical_shift,
            horizontal_shift=config.files.horizontal_shift,
            simulation_config=config.simulation,
            crop=config.files.crop,
        )
        dataset.psf = dataset.psf.to(device)
        psf = dataset.psf
        crop = dataset.crop

        # train-test split
        train_size = int((1 - config.files.test_size) * len(dataset))
        test_size = len(dataset) - train_size
        _, benchmark_dataset = torch.utils.data.random_split(
            dataset, [train_size, test_size], generator=generator
        )
        if config.n_files is not None:
            benchmark_dataset = Subset(benchmark_dataset, np.arange(config.n_files))
    else:
        raise ValueError(f"Dataset {dataset} not supported")

    print(f"Number of files : {len(benchmark_dataset)}")
    print(f"Data shape :  {dataset[0][0].shape}")

    model_list = []  # list of algoritms to benchmark
    if "ADMM" in config.algorithms:
        model_list.append(
            (
                "ADMM",
                ADMM(
                    psf,
                    mu1=config.admm.mu1,
                    mu2=config.admm.mu2,
                    mu3=config.admm.mu3,
                    tau=config.admm.tau,
                ),
            )
        )
    if "ADMM_Monakhova2019" in config.algorithms:
        model_list.append(("ADMM_Monakhova2019", ADMM(psf, mu1=1e-4, mu2=1e-4, mu3=1e-4, tau=2e-3)))
    if "FISTA" in config.algorithms:
        model_list.append(("FISTA", FISTA(psf, tk=config.fista.tk)))
    if "GradientDescent" in config.algorithms:
        model_list.append(("GradientDescent", GradientDescent(psf)))
    if "NesterovGradientDescent" in config.algorithms:
        model_list.append(
            (
                "NesterovGradientDescent",
                NesterovGradientDescent(psf, p=config.nesterov.p, mu=config.nesterov.mu),
            )
        )
    # APGD is not supported yet
    # if "APGD" in config.algorithms:
    #     from lensless import APGD

    #     model_list.append(("APGD", APGD(psf)))

    results = {}
    output_dir = None
    if config.save_idx is not None:

        assert np.max(config.save_idx) < len(
            benchmark_dataset
        ), "save_idx values must be smaller than dataset size"

        os.mkdir("GROUND_TRUTH")
        for idx in config.save_idx:
            ground_truth = benchmark_dataset[idx][1]
            ground_truth_np = ground_truth.cpu().numpy()[0]

            if crop is not None:
                ground_truth_np = ground_truth_np[
                    crop["vertical"][0] : crop["vertical"][1],
                    crop["horizontal"][0] : crop["horizontal"][1],
                ]

            save_image(
                ground_truth_np,
                fp=os.path.join("GROUND_TRUTH", f"{idx}.png"),
            )
    # benchmark each model for different number of iteration and append result to results
    # -- batchsize has to equal 1 as baseline models don't support batch processing
    start_time = time.time()
    for model_name, model in model_list:

        if config.save_idx is not None:
            # make directory for outputs
            os.mkdir(model_name)

        results[model_name] = dict()
        for n_iter in n_iter_range:

            print(f"Running benchmark for {model_name} with {n_iter} iterations")

            if config.save_idx is not None:
                output_dir = os.path.join(model_name, str(n_iter))
                os.mkdir(output_dir)

            result = benchmark(
                model,
                benchmark_dataset,
                batchsize=1,
                n_iter=n_iter,
                save_idx=config.save_idx,
                output_dir=output_dir,
                crop=crop,
            )
            results[model_name][int(n_iter)] = result

            # -- save results as easy to read JSON
            results_path = "results.json"
            with open(results_path, "w") as f:
                json.dump(results, f, indent=4)
    proc_time = (time.time() - start_time) / 60
    print(f"Total processing time: {proc_time:.2f} min")

    # create folder to load results from trained algorithms
    result_dir = os.path.join(get_original_cwd(), "benchmark", "trained_results")
    if not os.path.isdir(result_dir):
        os.mkdir(result_dir)

    # try to load json files with results form unrolled training
    files = glob.glob(os.path.join(result_dir, "*.json"))
    unrolled_results = {}
    for file in files:
        model_name = plib.Path(file).stem
        unrolled_results[model_name] = {}
        with open(file, "r") as f:
            result = json.load(f)

            # get result for each metric
            for metric in result.keys():
                # if list take last value (last epoch)
                if isinstance(result[metric], list):
                    unrolled_results[model_name][metric] = result[metric][-1]
                else:
                    unrolled_results[model_name][metric] = result[metric]

    # Baseline results
    baseline_label = config.baseline
    baseline_results = None
    if dataset == "DiffuserCam":
        # (Monakhova et al. 2019, https://arxiv.org/abs/1908.11502)
        # -- ADMM (100)
        if baseline_label == "MONAKHOVA 100iter":
            baseline_results = {
                "MSE": 0.0622,
                "LPIPS_Alex": 0.5711,
                "ReconstructionError": 13.62,
            }
        # -- ADMM (5)
        elif baseline_label == "MONAKHOVA 5iter":
            baseline_results = {
                "MSE": 0.1041,
                "LPIPS_Alex": 0.6309,
                "ReconstructionError": 11.32,
            }
        # -- Le-ADMM (Unrolled 5)
        elif baseline_label == "MONAKHOVA Unrolled 5iter":
            baseline_results = {
                "MSE": 0.0618,
                "LPIPS_Alex": 0.4434,
                "ReconstructionError": 13.70,
            }
        # -- Le-ADMM-U (Unrolled 5 + UNet post-denoiser)
        elif baseline_label == "MONAKHOVA Unrolled 5iter + UNet":
            baseline_results = {
                "MSE": 0.0074,
                "LPIPS_Alex": 0.1904,
                "ReconstructionError": 22.14,
            }
        else:
            raise ValueError(f"Baseline {baseline_label} not supported")

    # for each metrics plot the results comparing each model
    metrics_to_plot = ["SSIM", "PSNR", "MSE", "LPIPS_Vgg", "LPIPS_Alex", "ReconstructionError"]
    for metric in metrics_to_plot:
        plt.figure()
        # plot benchmarked algorithm
        for model_name in results.keys():
            plt.plot(
                n_iter_range,
                [results[model_name][n_iter][metric] for n_iter in n_iter_range],
                label=model_name,
            )
        # plot baseline as horizontal dotted line
        if baseline_results is not None:
            if metric in baseline_results.keys():
                plt.hlines(
                    baseline_results[metric],
                    0,
                    max(n_iter_range),
                    linestyles="dashed",
                    label=baseline_label,
                    color="orange",
                )

        # plot unrolled algorithms results
        color_list = ["red", "green", "blue", "orange", "purple"]
        algorithm_colors = {}
        for model_name in unrolled_results.keys():
            # use algorithm name if defined, else use file name
            if "algorithm" in unrolled_results[model_name].keys():
                plot_name = unrolled_results[model_name]["algorithm"]
            else:
                plot_name = model_name

            # set color depending on plot name using same color for same algorithm
            first = False
            if plot_name not in algorithm_colors.keys():
                algorithm_colors[plot_name] = color_list.pop()
                first = True
            color = algorithm_colors[plot_name]

            # check if metric is defined
            if metric not in unrolled_results[model_name].keys():
                continue
            # if n_iter is undefined, plot as horizontal line
            if "n_iter" not in unrolled_results[model_name].keys():
                plt.hlines(
                    unrolled_results[model_name][metric],
                    0,
                    n_iter_range[-1],
                    label=plot_name,
                    linestyles="dashed",
                    colors=color,
                )
            else:
                # plot as point
                if first:
                    plt.plot(
                        unrolled_results[model_name]["n_iter"],
                        unrolled_results[model_name][metric],
                        label=plot_name,
                        marker="o",
                        color=color,
                    )
                else:
                    plt.plot(
                        unrolled_results[model_name]["n_iter"],
                        unrolled_results[model_name][metric],
                        marker="o",
                        color=color,
                    )
        plt.xlabel("Number of iterations", fontsize="12")
        plt.ylabel(metric, fontsize="12")
        if metric == "ReconstructionError":
            plt.legend(fontsize="12")
        plt.grid()
        plt.savefig(f"{metric}.png")


if __name__ == "__main__":
    benchmark_recon()
