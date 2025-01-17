# #############################################################################
# benchmark.py
# =================
# Authors :
# Yohann PERRON
# Eric BEZZAM [ebezzam@gmail.com]
# #############################################################################


from lensless.utils.dataset import DiffuserCamTestDataset
from lensless.utils.io import save_image
from tqdm import tqdm
import os
import numpy as np

try:
    import torch
    from torch.utils.data import DataLoader
    from torch.nn import MSELoss, L1Loss
    from torchmetrics import StructuralSimilarityIndexMeasure
    from torchmetrics.image import lpip, psnr
except ImportError:
    raise ImportError(
        "Torch, torchvision, and torchmetrics are needed to benchmark reconstruction algorithm."
    )


def benchmark(
    model,
    dataset,
    batchsize=1,
    metrics=None,
    crop=None,
    save_idx=None,
    output_dir=None,
    **kwargs,
):
    """
    Compute multiple metrics for a reconstruction algorithm.

    Parameters
    ----------
    model : :py:class:`~lensless.ReconstructionAlgorithm`
        Reconstruction algorithm to benchmark.
    dataset : :py:class:`~lensless.benchmark.ParallelDataset`
        Parallel dataset of lensless and lensed images.
    batchsize : int, optional
        Batch size for processing. For maximum compatibility use 1 (batchsize above 1 are not supported on all algorithm), by default 1
    metrics : dict, optional
        Dictionary of metrics to compute. If None, MSE, MAE, SSIM, LPIPS and PSNR are computed.
    save_idx : list of int, optional
        List of indices to save the predictions, by default None (not to save any).
    output_dir : str, optional
        Directory to save the predictions, by default save in working directory if save_idx is provided.
    crop : dict, optional
        Dictionary of crop parameters (vertical: [start, end], horizontal: [start, end]), by default None (no crop).

    Returns
    -------
    Dict[str, float]
        A dictionnary containing the metrics name and average value
    """
    assert isinstance(model._psf, torch.Tensor), "model need to be constructed with torch support"
    device = model._psf.device

    if output_dir is None:
        output_dir = os.getcwd()
    else:
        output_dir = str(output_dir)
        if not os.path.exists(output_dir):
            os.mkdir(output_dir)

    if metrics is None:
        metrics = {
            "MSE": MSELoss().to(device),
            "MAE": L1Loss().to(device),
            "LPIPS_Vgg": lpip.LearnedPerceptualImagePatchSimilarity(
                net_type="vgg", normalize=True
            ).to(device),
            "LPIPS_Alex": lpip.LearnedPerceptualImagePatchSimilarity(
                net_type="alex", normalize=True
            ).to(device),
            "PSNR": psnr.PeakSignalNoiseRatio().to(device),
            "SSIM": StructuralSimilarityIndexMeasure().to(device),
            "ReconstructionError": None,
        }
    metrics_values = {key: 0.0 for key in metrics}

    # loop over batches
    dataloader = DataLoader(dataset, batch_size=batchsize, pin_memory=(device != "cpu"))
    model.reset()
    idx = 0
    for lensless, lensed in tqdm(dataloader):
        lensless = lensless.to(device)
        lensed = lensed.to(device)

        # compute predictions
        with torch.no_grad():
            if batchsize == 1:
                model.set_data(lensless)
                prediction = model.apply(plot=False, save=False, **kwargs)

            else:
                prediction = model.batch_call(lensless, **kwargs)

        # Convert to [N*D, C, H, W] for torchmetrics
        prediction = prediction.reshape(-1, *prediction.shape[-3:]).movedim(-1, -3)
        lensed = lensed.reshape(-1, *lensed.shape[-3:]).movedim(-1, -3)

        if crop is not None:
            prediction = prediction[
                ...,
                crop["vertical"][0] : crop["vertical"][1],
                crop["horizontal"][0] : crop["horizontal"][1],
            ]
            lensed = lensed[
                ...,
                crop["vertical"][0] : crop["vertical"][1],
                crop["horizontal"][0] : crop["horizontal"][1],
            ]

        if save_idx is not None:
            batch_idx = np.arange(idx, idx + batchsize)

            for i, idx in enumerate(batch_idx):
                if idx in save_idx:
                    prediction_np = prediction.cpu().numpy()[i]
                    # switch to [H, W, C] for saving
                    prediction_np = np.moveaxis(prediction_np, 0, -1)
                    save_image(prediction_np, fp=os.path.join(output_dir, f"{idx}.png"))

        # normalization
        prediction_max = torch.amax(prediction, dim=(-1, -2, -3), keepdim=True)
        if torch.all(prediction_max != 0):
            prediction = prediction / prediction_max
        else:
            print("Warning: prediction is zero")
        lensed_max = torch.amax(lensed, dim=(1, 2, 3), keepdim=True)
        lensed = lensed / lensed_max
        # compute metrics
        for metric in metrics:
            if metric == "ReconstructionError":
                metrics_values[metric] += model.reconstruction_error().cpu().item()
            else:
                if "LPIPS" in metric:
                    if prediction.shape[1] == 1:
                        # LPIPS needs 3 channels
                        metrics_values[metric] += (
                            metrics[metric](
                                prediction.repeat(1, 3, 1, 1), lensed.repeat(1, 3, 1, 1)
                            )
                            .cpu()
                            .item()
                        )
                    else:
                        metrics_values[metric] += metrics[metric](prediction, lensed).cpu().item()
                else:
                    metrics_values[metric] += metrics[metric](prediction, lensed).cpu().item()

        model.reset()
        idx += batchsize

    # average metrics
    for metric in metrics:
        metrics_values[metric] /= len(dataloader)

    return metrics_values


if __name__ == "__main__":
    from lensless import ADMM

    downsample = 1.0
    batchsize = 1
    n_files = 10
    n_iter = 100

    # check if GPU is available
    if torch.cuda.is_available():
        device = "cuda"
    else:
        device = "cpu"

    # prepare dataset
    dataset = DiffuserCamTestDataset(n_files=n_files, downsample=downsample)

    # prepare model
    psf = dataset.psf.to(device)
    model = ADMM(psf, n_iter=n_iter)

    # run benchmark
    print(benchmark(model, dataset, batchsize=batchsize))
