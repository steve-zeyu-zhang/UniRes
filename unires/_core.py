# Python
import os
# 3rd party
import torch
# NITorch
from nitorch.spatial import (affine_basis, affine_grid, grid_pull,
                             voxel_size, max_bb)
from nitorch.tools.preproc import (atlas_crop, affine_align,
                                   atlas_align, reset_origin)
from nitorch.plot.volumes import show_slices
from nitorch.io import map
from nitorch.core.math import round
from nitorch.core._linalg_expm import _expm
from nitorch.tools.img_statistics import (estimate_fwhm, estimate_noise)
from nitorch.core.constants import inf
# UniRes
from ._project import _proj_info
from .struct import (_input, _output)
from ._util import (_print_info, _read_image, _write_image)


def _all_mat_dim_vx(x, sett):
    """ Get all images affine matrices, dimensions and voxel sizes (as numpy arrays).

    Returns:
        all_mat (torch.tensor): Image orientation matrices (N, 4, 4).
        Dim (torch.tensor): Image dimensions (N, 3).
        all_vx (torch.tensor): Image voxel sizes (N, 3).

    """
    N = sum([len(xn) for xn in x])
    all_mat = torch.zeros((N, 4, 4), device=sett.device, dtype=torch.float64)
    all_dim = torch.zeros((N, 3), device=sett.device, dtype=torch.float64)
    all_vx = torch.zeros((N, 3), device=sett.device, dtype=torch.float64)

    cnt = 0
    for c in range(len(x)):
        for n in range(len(x[c])):
            all_mat[cnt, ...] = x[c][n].mat
            all_dim[cnt, ...] = torch.tensor(x[c][n].dim, 
                                         device=sett.device, dtype=torch.float64)
            all_vx[cnt, ...] = voxel_size(x[c][n].mat)
            cnt += 1

    return all_mat, all_dim, all_vx


def _crop_x(x, sett):
    """ Crop input images FOV to a specified bounding-box.

    Args:
        x (_input()): Input data.

    Returns:
        x (_input()): Cropped input data.

    """
    if not sett.crop:
        return x
    # Do cropping
    cnt = 0
    for c in range(len(x)):
        for n in range(len(x[c])):
            x[c][n].dat, x[c][n].mat, _ =  atlas_crop(
                [x[c][n].dat, x[c][n].mat], fov='brain', do_align=False)
            x[c][n].dim = x[c][n].dat.shape
            cnt += 1
    _print_info('crop', sett, cnt)

    return x


def _crop_y(y, sett):
    """ Crop output images FOV to a fixed dimension

    Args:
        y (_output()): _output data.

    Returns:
        y (_output()): Cropped output data.

    """
    if not sett.crop or sett.method == 'denoising':
        return y
    # y image information
    dim0 = torch.tensor(y[0].dim, device=y[0].dat.device)
    mat0 = y[0].mat
    vx0 = voxel_size(mat0)
    # Define cropped FOV
    dim_mni = torch.tensor([181, 217, 181], device=y[0].dat.device)  # FOV
    # based on nitorch/data/atlas_t1.nii.gz
    dim_mni = (dim_mni / vx0).round()  # Modulate with voxel size
    off = - (dim0 - dim_mni) / 2
    dim1 = dim0 + 2 * off
    mat_crop = torch.tensor([[1, 0, 0, - (off[0] + 1)],
                             [0, 1, 0, - (off[1] + 1)],
                             [0, 0, 1, - (off[2] + 1)],
                             [0, 0, 0, 1]], device=y[0].dat.device)
    mat1 = mat0.mm(mat_crop)
    dim1 = dim1.int().tolist()
    # Make output grid
    grid = affine_grid(mat_crop.type(y[0].dat.dtype), dim1)
    # Do interpolation
    for c in range(len(y)):
        dat = grid_pull(y[c].dat[None, None, ...],
                        grid[None, ...], bound='zero', extrapolate=False, interpolation=0)
        # Assign
        y[c].dat = dat[0, 0, ...]
        y[c].mat = mat1
        y[c].dim = dim1
    # show_slices(dat[0, 0, ...])

    return y


def _estimate_hyperpar(x, sett):
    """ Estimate noise precision (tau) and mean brain
        intensity (mu) of each observed image.

    Args:
        x (_input()): Input data.

    Returns:
        tau (list): List of C torch.tensor(float) with noise precision of each MR image.
        lam (torch.tensor(float)): The parameter lambda (1, C).

    """
    # Print info to screen
    t0 = _print_info('hyper_par', sett)
    # Total number of observations
    N = sum([len(xn) for xn in x])
    # Do estimation
    cnt = 0
    for c in range(len(x)):
        for n in range(len(x[c])):
            # Get data
            dat = x[c][n].dat
            if x[c][n].ct:
                # Estimate noise sd from estimate of FWHM
                sd_bg = estimate_fwhm(dat, voxel_size(x[c][n].mat), mn=20, mx=50)[1]
                mu_bg = torch.tensor(0.0, device=dat.device, dtype=dat.dtype)
                mu_fg = torch.tensor(sett.reg_scl/8.0 * 1024.0, device=dat.device, dtype=dat.dtype)
                if N > 1:
                    mu_fg /= 20
            else:
                # Get noise and foreground statistics
                sd_bg, sd_fg, mu_bg, mu_fg = estimate_noise(dat, num_class=2, show_fit=sett.show_hyperpar,
                                                            fig_num=100 + cnt)
                mu_bg = torch.tensor(0.0, device=dat.device, dtype=dat.dtype)
            # Set values
            x[c][n].sd = sd_bg.float()
            x[c][n].tau = 1 / sd_bg.float() ** 2
            x[c][n].mu = torch.abs(mu_fg.float() - mu_bg.float())
            cnt += 1

    # Print info to screen
    _print_info('hyper_par', sett, x, t0)

    return x


def _fix_affine(x, sett):
    """Fix messed up affine in CT scans.

    """
    cnt = 0
    for c in range(len(x)):
        for n in range(len(x[c])):
            if x[c][n].ct and sett.do_res_origin:
                x[c][n].dat, x[c][n].mat, _ = reset_origin(
                    [x[c][n].dat, x[c][n].mat], device=sett.device)
                x[c][n].dim = x[c][n].dat.shape
                cnt += 1
    _print_info('fix-affine', sett, cnt)

    return x


def _format_y(x, sett):
    """ Construct algorithm output struct. See _output() dataclass.

    Returns:
        y (_output()): Algorithm output struct(s).

    """
    one = torch.tensor(1.0, device=sett.device, dtype=torch.float64)
    vx_y = sett.vx
    if vx_y is not None:
        if isinstance(vx_y, int):
            vx_y = float(vx_y)
        if isinstance(vx_y, float):
            vx_y = (vx_y,) * 3
        vx_y = torch.tensor(vx_y, dtype=torch.float64, device=sett.device)

    # Get all orientation matrices and dimensions
    all_mat, all_dim, all_vx = _all_mat_dim_vx(x, sett)
    N = all_mat.shape[0]  # Total number of observations

    if N == 1:
        # Disable unified rigid registration
        sett.unified_rigid = False
        sett.clean_fov = True

    # Check if all input images have the same fov/vx
    mat_same = True
    dim_same = True
    vx_same = True
    for n in range(1, N):
        mat_same = mat_same & \
            torch.equal(round(all_mat[n - 1, ...], 3), round(all_mat[n, ...], 3))
        dim_same = dim_same & \
            torch.equal(round(all_dim[n - 1, ...], 3), round(all_dim[n, ...], 3))
        vx_same = vx_same & \
            torch.equal(round(all_vx[n - 1, ...], 3), round(all_vx[n, ...], 3))

    # Decide if super-resolving and/or projection is necessary
    do_sr = True
    sett.do_proj = True
    if vx_y is None and ((N == 1) or vx_same):  # One image, voxel size not given
        vx_y = all_vx[0, ...]

    if vx_same and (torch.abs(all_vx[0, ...] - vx_y) < 1e-3).all():
        # All input images have same voxel size, and output voxel size is the also the same
        do_sr = False
        if mat_same and dim_same and not sett.unified_rigid:
            # All input images have the same FOV
            mat = all_mat[0, ...]
            dim = all_dim[0, ...]
            sett.do_proj = False

    if do_sr or sett.do_proj:
        # Get FOV of mean space
        mat, dim = max_bb(all_mat, all_dim, vx_y)

    # Set method
    if do_sr:
        sett.method = 'super-resolution'
    else:
        sett.method = 'denoising'

    # Optimise even/odd scaling parameter?
    if sett.method == 'denoising' or (N == 1 and x[0][0].ct):
        sett.scaling = False

    dim = tuple(dim.int().tolist())
    _ = _print_info('mean-space', sett, dim, mat)

    # Assign output
    y = []
    for c in range(len(x)):
        y.append(_output())
        # Regularisation (lambda) for channel c
        mu_c = torch.zeros(len(x[c]), dtype=torch.float32, device=sett.device)
        for n in range(len(x[c])):
            mu_c[n] = x[c][n].mu
        y[c].lam0 = 1 / torch.mean(mu_c)
        y[c].lam = 1 / torch.mean(mu_c)  # To facilitate rescaling
        # Output image(s) dimension and orientation matrix
        y[c].dim = dim
        y[c].mat = mat.double().to(sett.device)

    return y, sett


def _get_sched(N, sett):
    """ Define a coarse-to-fine scaling of regularisation

    """
    # Parameters/settings
    if sett.sched_num < 0 or N == 1:
        sett.sched_num = 0
    if sett.rigid_mod < 1:
        sett.rigid_mod = 1
    scl = sett.reg_scl
    two = torch.tensor(2.0, device=sett.device, dtype=torch.float32)
    # Build scheduler
    sched = two ** torch.arange(0, 32, step=1,
                                device=sett.device, dtype=torch.float32).flip(dims=(0,))
    ix = torch.min((sched - sett.reg_scl).abs(), dim=0)[1]
    sched = sched[ix - sett.sched_num:ix]
    sched = torch.cat((sched, scl.reshape(1)))
    sett.reg_scl = sched

    return sett


def _init_reg(x, sett):
    """ Initialise registration.

    """
    # Total number of observations
    N = sum([len(xn) for xn in x])
    # Set rigid affine basis
    sett.rigid_basis = affine_basis(group='SE', device=sett.device,
                                    dtype=torch.float64)
    fix = 0  # Fixed image index
    # Make input for nitorch affine align
    imgs = []
    for c in range(len(x)):
        for n in range(len(x[c])):
            imgs.append([x[c][n].dat, x[c][n].mat])

    if sett.do_coreg and N > 1:
        # Align images, pairwise, to fixed image (fix)
        t0 = _print_info('init-reg', sett, 'co', 'begin')
        mat_a = affine_align(imgs, fix=fix)[1]
        for i in range(len(imgs)):
            imgs[i][1] = imgs[i][1].solve(mat_a[i, ...])[0]
        _print_info('init-reg', sett, 'co', 'finished', t0)

    if sett.do_atlas_align:
        # Align fixed image to atlas space, and apply transformation to
        # all images
        t0 = _print_info('init-reg', sett, 'atlas', 'begin')
        imgs1 = [imgs[fix]]
        _, mat_a, _ = atlas_align(imgs1, rigid=sett.atlas_rigid)
        # Apply atlas registration transform
        for i in range(len(imgs)):
            imgs[i][1] = imgs[i][1].solve(mat_a)[0]
        _print_info('init-reg', sett, 'atlas', 'finished', t0)

    # Modify image affine
    cnt = 0
    for c in range(len(x)):
        for n in range(len(x[c])):
            x[c][n].mat = imgs[cnt][1]
            cnt += 1

    # Init rigid parameters (for unified rigid registration)
    for c in range(len(x)):  # Loop over channels
        for n in range(len(x[c])):  # Loop over observations of channel c
            x[c][n].rigid_q = torch.zeros(sett.rigid_basis.shape[0],
                device=sett.device, dtype=torch.float64)

    return x, sett


def _init_y_dat(x, y, sett):
    """ Make initial guesses of reconstucted image(s) using b-spline interpolation,
        with averaging if more than one observation per channel.

    """
    dim_y = x[0][0].po.dim_y
    mat_y = x[0][0].po.mat_y
    for c in range(len(x)):
        dat_y = torch.zeros(dim_y, dtype=torch.float32, device=sett.device)
        num_x = len(x[c])
        for n in range(num_x):
            # Get image data
            dat = x[c][n].dat[None, None, ...]
            # Make output grid
            mat = mat_y.solve(x[c][n].po.mat_x)[0]  # mat_x\mat_y
            grid = affine_grid(mat.type(dat.dtype), x[c][n].po.dim_y)
            # Do interpolation
            mn = torch.min(dat)
            mx = torch.max(dat)
            dat = grid_pull(dat, grid[None, ...], bound='zero', extrapolate=False, interpolation=1)
            dat[dat < mn] = mn
            dat[dat > mx] = mx
            dat_y = dat_y + dat[0, 0, ...]
        y[c].dat = dat_y / num_x

    return y


def _proj_info_add(x, y, sett):
    """ Adds a projection matrix encoding to each input (x).

    """
    # Build each projection operator
    for c in range(len(x)):
        dim_y = y[c].dim
        mat_y = y[c].mat
        for n in range(len(x[c])):
            # Get rigid matrix
            rigid = _expm(x[c][n].rigid_q, sett.rigid_basis)
            # Define projection operator
            x[c][n].po = _proj_info(dim_y, mat_y, x[c][n].dim, x[c][n].mat,
                                   prof_ip=sett.profile_ip, prof_tp=sett.profile_tp,
                                   gap=sett.gap, device=sett.device, rigid=rigid)

    return x


def _read_data(data, sett):
    """ Parse input data into algorithm input struct(s).

    Args:
        data

    Returns:
        x (_input()): Algorithm input struct(s).

    """
    # Sanity check
    mat_vol = sett.mat
    if isinstance(data, str):
        file = map(data)
        dim = file.shape
        if len(dim) > 3:
            # Data is path to 4D nifti
            data = file.fdata()
            mat_vol = file.affine
    try:
        data.shape
        data = data[..., None]
        data = data[:, :, :, :, 0]
        if mat_vol is None:
            raise ValueError('Image data given as array, please also provide affine matrix in sett.mat!')
    except AttributeError:
        pass
    if isinstance(data, str):
        data = [data]

    # Number of channels
    if mat_vol is not None:
        C = data.shape[3]
    else:
        C = len(data)

    x = []
    for c in range(C):  # Loop over channels
        x.append([])
        x[c] = []
        if mat_vol is None and isinstance(data[c], list) and (isinstance(data[c][0], str) or isinstance(data[c][0], list)):
            # Possibly multiple repeats per channel
            for n in range(len(data[c])):  # Loop over observations of channel c
                x[c].append(_input())
                # Get data
                dat, dim, mat, fname, direc, nam, file, ct = \
                    _read_image(data[c][n], sett.device, is_ct=sett.has_ct)
                # Assign
                x[c][n].dat = dat
                x[c][n].dim = dim
                x[c][n].mat = mat
                x[c][n].fname = fname
                x[c][n].direc = direc
                x[c][n].nam = nam
                x[c][n].file = file
                x[c][n].ct = ct
        else:
            # One repeat per channel
            n = 0
            x[c].append(_input())
            # Get data
            if mat_vol is not None:
                dat, dim, mat, fname, direc, nam, file, ct = \
                    _read_image([data[..., c], mat_vol], sett.device, is_ct=sett.has_ct)
            else:
                dat, dim, mat, fname, direc, nam, file, ct = \
                    _read_image(data[c], sett.device, is_ct=sett.has_ct)
            # Assign
            x[c][n].dat = dat
            x[c][n].dim = dim
            x[c][n].mat = mat
            x[c][n].fname = fname
            x[c][n].direc = direc
            x[c][n].nam = nam
            x[c][n].file = file
            x[c][n].ct = ct

    return x


def _write_data(x, y, sett, jtv=None):
    """ Format algorithm output.

    Args:
        jtv (torch.tensor, optional): Joint-total variation image, defaults to None.

    Returns:
        dat_y (torch.tensor): Reconstructed image data, (dim_y, C).
        pth_y ([str, ...]): Paths to reconstructed images.

    """
    # Output orientation matrix
    mat = y[0].mat
    dir_out = sett.dir_out
    if dir_out is None:
        # No output directory given, use directory of input data
        if x[0][0].direc is None:
            dir_out = 'UniRes-output'
        else:
            dir_out = x[0][0].direc
    if not os.path.isdir(dir_out):
        os.makedirs(dir_out, exist_ok=True)
    # Reconstructed images
    prefix_y = sett.prefix
    pth_y = []
    for c in range(len(x)):
        dat = y[c].dat
        mn = inf
        mx = -inf
        for n in range(len(x[c])):
            if torch.min(x[c][n].dat) < mn:
                mn = torch.min(x[c][n].dat)
            if torch.max(x[c][n].dat) > mx:
                mx = torch.max(x[c][n].dat)
        dat[dat < mn] = mn
        dat[dat > mx] = mx
        if sett.write_out and sett.mat is None:
            # Write reconstructed images (as separate niftis, because given as separate niftis)
            if x[c][0].nam is None:
                nam = str(c) + '.nii'
            else:
                nam = x[c][0].nam
            fname = os.path.join(dir_out, prefix_y + nam)
            pth_y.append(fname)
            _write_image(dat, fname, mat=mat, file=x[c][0].file)
        if c == 0:
            dat_y = dat[..., None].clone()
        else:
            dat_y = torch.cat((dat_y, dat[..., None]), dim=3)
    if sett.write_out and sett.mat is not None:
        # Write reconstructed images as 4D volume (because given as 4D volume)
        c = 0
        if x[c][0].nam is None:
            nam = str(c) + '.nii'
        else:
            nam = x[c][0].nam
        fname = os.path.join(dir_out, prefix_y + nam)
        pth_y.append(fname)
        _write_image(dat_y, fname, mat=mat, file=x[c][0].file)
    if sett.write_jtv and jtv is not None:
        # Write JTV
        if x[c][0].nam is None:
            nam = str(c) + '.nii'
        else:
            nam = x[c][0].nam
        fname = os.path.join(dir_out, 'jtv_' + prefix_y + nam)
        _write_image(jtv, fname, mat=mat)

    return dat_y, pth_y
