# #############################################################################
# mask.py
# =================
# Authors :
# Aaron FARGEON [aa.fargeon@gmail.com]
# Eric BEZZAM [ebezzam@gmail.com]
# #############################################################################

"""
Mask
====

This module provides utilities to create different types of masks (:py:class:`~lensless.hardware.mask.CodedAperture`,
:py:class:`~lensless.hardware.mask.PhaseContour`,
:py:class:`~lensless.hardware.mask.FresnelZoneAperture`) and simulate the corresponding PSF.

"""


import abc
import warnings
import numpy as np
import cv2 as cv
from math import sqrt
from perlin_numpy import generate_perlin_noise_2d
from sympy.ntheory import quadratic_residues
from scipy.signal import max_len_seq
from scipy.linalg import circulant
from numpy.linalg import multi_dot
from waveprop.fresnel import fresnel_conv
from waveprop.rs import angular_spectrum
from waveprop.noise import add_shot_noise
from lensless.hardware.sensor import VirtualSensor
from lensless.utils.image import resize
from matplotlib import pyplot as plt

try:
    import torch

    torch_available = True
except ImportError:
    torch_available = False


class Mask(abc.ABC):
    """
    Parent ``Mask`` class. Attributes common to each type of mask.
    """

    def __init__(
        self,
        resolution,
        distance_sensor,
        size=None,
        feature_size=None,
        psf_wavelength=[460e-9, 550e-9, 640e-9],
        **kwargs
    ):
        """
        Constructor from parameters of the user's choice.

        Parameters
        ----------
        resolution: array_like
            Resolution of the  mask (px).
        distance_sensor: float
            Distance between the mask and the sensor (m).
        size: array_like
            Size of the sensor (m). Only one of ``size`` or ``feature_size`` needs to be specified.
        feature_size: float or array_like
            Size of the feature (m). Only one of ``size`` or ``feature_size`` needs to be specified.
        psf_wavelength: list, optional
            List of wavelengths to simulate PSF (m). Default is [460e-9, 550e-9, 640e-9] nm (blue, green, red).
        """

        resolution = np.array(resolution)
        assert len(resolution) == 2, "Sensor resolution should be of length 2"

        assert (
            size is not None or feature_size is not None
        ), "Either sensor_size or feature_size should be specified"
        if size is None:
            size = np.array(resolution * feature_size)
        else:
            size = np.array(size)
            assert len(size) == 2, "Sensor size should be of length 2"
        if feature_size is None:
            feature_size = np.array(size) / np.array(resolution)
        else:
            if isinstance(feature_size, float):
                feature_size = np.array([feature_size, feature_size])
            else:
                assert len(feature_size) == 2, "Feature size should be of length 2"
                feature_size = np.array(feature_size)
            assert np.all(feature_size > 0), "Feature size should be positive"
        assert np.all(resolution * feature_size <= size)

        self.phase_mask = None
        self.resolution = resolution
        self.size = size
        if feature_size is None:
            self.feature_size = self.size / self.resolution
        else:
            self.feature_size = feature_size
        self.distance_sensor = distance_sensor

        # create mask
        self.mask = None
        self.create_mask()
        self.shape = self.mask.shape

        # PSF
        self.psf_wavelength = psf_wavelength
        self.psf = None
        self.compute_psf()

    @classmethod
    def from_sensor(cls, sensor_name, downsample=None, **kwargs):
        """
        Constructor from an existing virtual sensor that copies over the sensor parameters
        (sensor resolution, sensor size, feature size).

        Parameters
        ----------
        sensor_name: str
            Name of the sensor. See :py:class:`~lensless.hardware.sensor.SensorOptions`.
        downsample: float, optional
            Downsampling factor.
        **kwargs:
            Additional arguments for the mask constructor. See the abstract class :py:class:`~lensless.hardware.mask.Mask`
            and the corresponding subclass for more details.

        Example
        -------

        .. code-block:: python

            mask = CodedAperture.from_sensor(sensor_name=SensorOptions.RPI_HQ, downsample=8, ...)
        """
        sensor = VirtualSensor.from_name(sensor_name, downsample)
        return cls(
            resolution=tuple(sensor.resolution.copy()),
            size=tuple(sensor.size.copy()),
            feature_size=sensor.pixel_size.copy(),
            **kwargs
        )

    @abc.abstractmethod
    def create_mask(self):
        """
        Abstract mask creation method that creates mask with subclass-specific function.
        """
        pass

    def compute_psf(self):
        """
        Compute the intensity PSF with bandlimited angular spectrum (BLAS) for each wavelength.
        Common to all types of masks.
        """
        psf = np.zeros(tuple(self.resolution) + (len(self.psf_wavelength),), dtype=np.complex64)
        for i, wv in enumerate(self.psf_wavelength):
            psf[:, :, i] = angular_spectrum(
                u_in=self.mask,
                wv=wv,
                d1=self.feature_size,
                dz=self.distance_sensor,
                dtype=np.float32,
                bandlimit=True,
            )[0]

        # intensity PSF
        self.psf = np.abs(psf) ** 2


class CodedAperture(Mask):
    """
    Coded aperture mask as in `FlatCam <https://arxiv.org/abs/1509.00116>`_.
    """

    def __init__(self, method="MLS", n_bits=8, **kwargs):
        """
        Coded aperture mask contructor (FlatCam).

        Parameters
        ----------
        method: str
            Pattern generation method (MURA or MLS). Default is ``MLS``.
        n_bits: int, optional
            Number of bits for pattern generation.
            Size is ``4*n_bits + 1`` for MURA and ``2^n - 1`` for MLS.
            Default is 8 (for a 255x255 MLS mask).
        **kwargs:
            The keyword arguments are passed to the parent class :py:class:`~lensless.hardware.mask.Mask`.
        """

        self.row = None
        self.col = None
        self.method = method
        self.n_bits = n_bits

        super().__init__(**kwargs)

    def create_mask(self):
        """
        Creating coded aperture mask using either the MURA of MLS method.
        """
        assert self.method.upper() in ["MURA", "MLS"], "Method should be either 'MLS' or 'MURA'"

        # Generating pattern
        if self.method.upper() == "MURA":
            self.mask = self.squarepattern(4 * self.n_bits + 1)[1:, 1:]
            self.row = 2 * self.mask[0, :] - 1
            self.col = 2 * self.mask[:, 0] - 1
        else:
            seq = max_len_seq(self.n_bits)[0] * 2 - 1
            h_r = np.r_[seq, seq]
            self.row = h_r
            self.col = h_r
            self.mask = (np.outer(h_r, h_r) + 1) / 2

        # Upscaling
        if np.any(self.resolution != self.mask.shape):
            upscaled_mask = resize(
                self.mask[:, :, np.newaxis], shape=tuple(self.resolution) + (1,)
            ).squeeze()
            upscaled_mask = np.clip(upscaled_mask, 0, 1)
            self.mask = np.round(upscaled_mask).astype(int)

    def is_prime(self, n):
        """
        Assess whether a number is prime or not.

        Parameters
        ----------
        n: int
            The number we want to check.
        """
        if n % 2 == 0 and n > 2:
            return False
        return all(n % i for i in range(3, int(sqrt(n)) + 1, 2))

    def squarepattern(self, p):
        """
        Generate MURA square pattern.

        Parameters
        ----------
        p: int
            Number of bits.
        """
        if not self.is_prime(p):
            raise ValueError("p is not a valid length. It must be prime.")
        A = np.zeros((p, p), dtype=int)
        q = quadratic_residues(p)
        A[1:, 0] = 1
        for j in range(1, p):
            for i in range(1, p):
                if not ((i - 1 in q) != (j - 1 in q)):
                    A[i, j] = 1
        return A

    def get_conv_matrices(self, img_shape):
        """
        Get theoretical left and right convolution matrices for the separable mask.

        Such that measurement model is given ``P @ img @ Q.T``.

        Parameters
        ----------
        img_shape: tuple
            Shape of the image to being convolved.

        Returns
        -------
        P: :py:class:`~numpy.ndarray`
            Left convolution matrix.
        Q: :py:class:`~numpy.ndarray`
            Right convolution matrix.

        """

        P = circulant(np.resize(self.col, self.resolution[0]))[:, : img_shape[0]]
        Q = circulant(np.resize(self.row, self.resolution[1]))[:, : img_shape[1]]

        return P, Q

    def simulate(self, obj, snr_db=20):
        """
        Simulate the mask measurement of an image. Apply left and right convolution matrices,
        add noise and return the measurement.

        Parameters
        ----------
        obj: :py:class:`~numpy.ndarray`
            Image to simulate.
        snr_db: float, optional
            Signal-to-noise ratio (dB) of the simulated measurement. Default is 20 dB.
        """
        assert len(obj.shape) == 3, "Object should be a 3D array (HxWxC) even if grayscale."

        # Get convolution matrices
        P, Q = self.get_conv_matrices(obj.shape)

        # Convolve image
        n_channels = obj.shape[-1]

        if torch_available and isinstance(obj, torch.Tensor):
            P = torch.from_numpy(P).float()
            Q = torch.from_numpy(Q).float()
            meas = torch.dstack(
                [torch.linalg.multi_dot([P, obj[:, :, c], Q.T]) for c in range(n_channels)]
            ).float()
        else:
            meas = np.dstack([multi_dot([P, obj[:, :, c], Q.T]) for c in range(n_channels)])

        # Add noise
        if snr_db is not None:
            meas = add_shot_noise(meas, snr_db=snr_db)

        if torch_available and isinstance(obj, torch.Tensor):
            meas = meas.to(obj)

        return meas

class MultiLensArray(Mask):
    """
    Multi-lens array mask.
    """
    def __init__(
        self, N = None, radius = None, loc = None, refractive_index = 1.2, design_wv=532e-9, seed = 0, min_height=1e-3, **kwargs
    ):
        """
        Multi-lens array mask constructor.

        Parameters
        ----------
        N: int
            Number of lenses
        radius: array_like
            Radius of the lenses (m)
        loc: array_like of tuples
            Location of the lenses (m)
        refractive_index: float
            Refractive index of the mask substrate. Default is 1.2.
        wavelength: float
        seed: int
            Seed for the random number generator. Default is 0.
        min_height: float
            Minimum height of the lenses (m). Default is 1e-3.
        """
        self.N = N
        self.radius = radius
        self.loc = loc
        self.refractive_index = refractive_index
        self.wavelength = design_wv
        self.seed = seed
        self.min_height = min_height
        
        if self.radius is not None:
            assert self.loc is not None
            assert len(self.radius) == len(self.loc)
            self.N = len(self.radius)
            circles = np.array([(self.loc[i][0], self.loc[i][1], self.radius[i]) for i in range(self.N)])
            assert MultiLensArray.no_circle_overlap(circles)
        else:
            assert self.N is not None
            np.random.seed(self.seed)
            self.radius = np.random.uniform(self.min_height, 1e-5, self.N) #TODO: check if it is the right way to do it
            assert self.N == len(self.radius)
        super().__init__(**kwargs)
    
        

    @staticmethod
    def no_circle_overlap(circles):
        """Check if any circle in the list overlaps with another."""
        for i in range(len(circles)):
            if MultiLensArray.does_circle_overlap(circles[i+1:], circles[i][0], circles[i][1], circles[i][2]):
                return False
        return True
    
    @staticmethod
    def does_circle_overlap(circles, x, y, r):
        """Check if a circle overlaps with any in the list."""
        for (cx, cy, cr) in circles:
            if np.sqrt((x - cx)**2 + (y - cy)**2) < r + cr:
                return True
        return False

    def place_spheres_on_plane(self, width, height, radius, max_attempts=1000):
        """Try to place circles on a 2D plane."""
        placed_circles = []
        placed_circles_res = []
        radius_sorted = sorted(radius, reverse=True)  # Place larger circles first

        for r in radius_sorted:
            placed = False
            for _ in range(max_attempts):
                x = np.random.uniform(r, width - r)
                y = np.random.uniform(r, height - r)
            
                if not MultiLensArray.does_circle_overlap(placed_circles, x , y , r):
                    placed_circles.append((x, y, r))
                    placed_circles_res.append((x / self.feature_size[0], y / self.feature_size[1], r / self.feature_size[0]))
                    placed = True
                    print(f"Placed circle with rad {r}, and center ({x}, {y})")
                    break
        
            if not placed:
                print(f"Failed to place circle with rad {r}")
                continue

        placed_circles = np.array(placed_circles)
        circles = placed_circles[:, :2]
        radius = placed_circles[:, 2]
        return circles, radius

    def create_mask(self):
        if self.loc is None:
            self.loc, self.radius = self.place_spheres_on_plane(self.size[0], self.size[1], self.radius)
        locs_res = self.loc * (1/self.feature_size)
        radius_res = self.radius * (1/self.feature_size[0]) 
        height = self.create_height_map(radius_res, locs_res)
        
        phi = (height * (self.refractive_index - 1) * 2 * np.pi / self.wavelength) #% (2*np.pi)#? Makes it have some noisy values instead of a continuous sphere
        fig, ax = plt.subplots()
        im = ax.imshow(phi, cmap="gray")
        fig.colorbar(im, ax=ax, shrink=0.5, aspect=5)
        plt.show()
        self.mask = np.exp(1j * phi)

    def create_height_map(self, radius, locs):
        height = np.full((self.resolution[0], self.resolution[1]), self.min_height)
        for x in range(height.shape[0]):
            for y in range(height.shape[1]):
                height[x, y] += self.lens_contribution(radius, locs, (x + 0.5), (y + 0.5)) * self.feature_size[0]
        assert np.all(height >= self.min_height)
        return height
    
    def lens_contribution(self, radius, locs, x, y):
        contribution = 0
        for idx, loc in enumerate(locs):
            if (x-loc[0])**2 + (y-loc[1])**2 < radius[idx]**2:
                contribution = np.sqrt((radius[idx])**2 - ((x-loc[0]))**2 - ((y -loc[1]))**2)
                return contribution
        return contribution


class PhaseContour(Mask):
    """
    Phase contour mask as in `PhlatCam <https://ieeexplore.ieee.org/document/9076617>`_.
    """

    def __init__(
        self, noise_period=(16, 16), refractive_index=1.2, n_iter=10, design_wv=532e-9, **kwargs
    ):
        """
        Phase contour mask contructor (PhlatCam).

        Parameters
        ----------
        noise_period: array_like, optional
            Noise period of the Perlin noise (px). Default is (8, 8).
        refractive_index: float, optional
            Refractive index of the mask substrate. Default is 1.2.
        n_iter: int, optional
            Number of iterations for the phase retrieval algorithm. Default is 10.
        design_wv: float, optional
            Wavelength used to design the mask (m). Default is 532e-9, as in the PhlatCam paper.
        **kwargs:
            The keyword arguments are passed to the parent class :py:class:`~lensless.hardware.mask.Mask`.
        """

        self.target_psf = None
        self.phase_pattern = None
        self.height_map = None
        self.noise_period = noise_period
        self.refractive_index = refractive_index
        self.n_iter = n_iter
        self.design_wv = design_wv
        

        super().__init__(**kwargs)

    def create_mask(self):
        """
        Creating phase contour from edges of Perlin noise.
        """

        # Creating Perlin noise
        proper_dim_1 = (self.resolution[0] // self.noise_period[0]) * self.noise_period[0]
        proper_dim_2 = (self.resolution[1] // self.noise_period[1]) * self.noise_period[1]
        noise = generate_perlin_noise_2d((proper_dim_1, proper_dim_2), self.noise_period)

        # Upscaling to correspond to sensor size
        if np.any(self.resolution != noise.shape):
            noise = resize(noise[:, :, np.newaxis], shape=tuple(self.resolution) + (1,)).squeeze()

        # Edge detection
        binary = np.clip(np.round(np.interp(noise, (-1, 1), (0, 1))), a_min=0, a_max=1)
        self.target_psf = cv.Canny(np.interp(binary, (-1, 1), (0, 255)).astype(np.uint8), 0, 255)

        # Computing mask and height map
        phase_mask, height_map = phase_retrieval(
            target_psf=self.target_psf,
            wv=self.design_wv,
            d1=self.feature_size,
            dz=self.distance_sensor,
            n=self.refractive_index,
            n_iter=self.n_iter,
            height_map=True,
        )
        self.height_map = height_map
        self.phase_pattern = phase_mask
        self.mask = np.exp(1j * phase_mask)


def phase_retrieval(target_psf, wv, d1, dz, n=1.2, n_iter=10, height_map=False):
    """
    Iterative phase retrieval algorithm similar to `PhlatCam <https://ieeexplore.ieee.org/document/9076617>`_,
    using Fresnel propagation.

    Parameters
    ----------
    target_psf: array_like
        Target PSF to optimize the phase mask for.
    wv: float
        Wavelength (m).
    d1: float=
        Sample period on the sensor i.e. pixel size (m).
    dz: float
        Propagation distance between the mask and the sensor.
    n: float
        Refractive index of the mask substrate. Default is 1.2.
    n_iter: int
        Number of iterations. Default value is 10.
    """

    M_p = np.sqrt(target_psf)

    if hasattr(d1, "__len__"):
        if d1[0] != d1[1]:
            warnings.warn("Non-square pixel, first dimension taken as feature size.")
        d1 = d1[0]

    for _ in range(n_iter):
        # back propagate from sensor to mask
        M_phi = fresnel_conv(M_p, wv, d1, -dz, dtype=np.float32)[0]
        # constrain amplitude at mask to be unity, i.e. phase pattern
        M_phi = np.exp(1j * np.angle(M_phi))
        # forward propagate from mask to sensor
        M_p = fresnel_conv(M_phi, wv, d1, dz, dtype=np.float32)[0]
        # constrain amplitude to be sqrt(PSF)
        M_p = np.sqrt(target_psf) * np.exp(1j * np.angle(M_p))

    phi = (np.angle(M_phi) + 2 * np.pi) % (2 * np.pi)

    if height_map:
        return phi, wv * phi / (2 * np.pi * (n - 1))
    else:
        return phi


class FresnelZoneAperture(Mask):
    """
    Fresnel Zone Aperture (FZA) mask as in `this work <https://www.nature.com/articles/s41377-020-0289-9>`_,
    namely binarized cosine function.
    """

    def __init__(self, radius=0.32e-3, **kwargs):
        """
        Fresnel Zone Aperture mask contructor.

        Parameters
        ----------
        radius: float
            characteristic radius of the FZA (m)
            default value: 5e-4
        **kwargs:
            The keyword arguments are passed to the parent class :py:class:`~lensless.hardware.mask.Mask`.
        """

        self.radius = radius

        super().__init__(**kwargs)

    def create_mask(self):
        """
        Creating binary Fresnel Zone Aperture mask.
        """
        dim = self.resolution
        x, y = np.meshgrid(
            np.linspace(-dim[1] / 2, dim[1] / 2 - 1, dim[1]),
            np.linspace(-dim[0] / 2, dim[0] / 2 - 1, dim[0]),
        )
        radius_px = self.radius / self.feature_size[0]
        mask = 0.5 * (1 + np.cos(np.pi * (x**2 + y**2) / radius_px**2))
        self.mask = np.round(mask)


class HeightVarying(Mask):
    """
    A class representing a height-varying mask for lensless imaging.

    Parameters
    ----------
    refractive_index : float, optional
        The refractive index of the material. Default is 1.2.
    wavelength : float, optional
        The wavelength of the light. Default is 532e-9.
    height_map : ndarray or None, optional
        An array representing the height map of the mask. If None, a random height map is generated.
    height_range : tuple, optional
        A tuple (min, max) specifying the range of heights when generating a random height map.
        Default is (min, max), where min and max are placeholders for the actual values.
    seed : int, optional
        Seed for the random number generator when generating a random height map. Default is 0.

    Example
    -------
    Creating an instance with a custom height map:

    >>> custom_height_map = np.array([0.1, 0.2, 0.3])
    >>> height_varying_instance = HeightVarying(
    ...     refractive_index=1.2,
    ...     wavelength=532e-9,
    ...     height_map=custom_height_map,
    ...     height_range=(0.0, 1.0),
    ...     seed=42
    ... )
    """
    def __init__(
            self, 
            refractive_index = 1.2, 
            wavelength = 532e-9, 
            height_map = None,
            height_range = (1e-5, 1e-3), 
            seed = 0,
            **kwargs):
        
        
        self.refractive_index = refractive_index
        self.wavelength = wavelength
        self.height_range = height_range
        self.seed = seed

        if height_map is not None:
            self.height_map = height_map
        else:
            self.height_map = None
            np.random.seed(self.seed)

        super().__init__(**kwargs)

    def get_phi(self):
        phi = self.height_map * (2*np.pi*(self.refractive_index-1) / self.wavelength)
        phi = phi % (2*np.pi)
        return phi
    
    def create_mask(self):
        if self.height_map is None:
            self.height_map = np.random.uniform(self.height_range[0], self.height_range[1], self.resolution)
        assert self.height_map.shape == tuple(self.resolution)
        phase_mask = self.get_phi()
        self.mask = np.exp(1j * phase_mask)