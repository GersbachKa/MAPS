import numpy as np, sympy as sp, scipy.special as scsp
import scipy.optimize as sopt

import pickle, healpy as hp

import numpy.random as nr, scipy.stats as scst
from PTMCMCSampler.PTMCMCSampler import PTSampler as ptmcmc

from enterprise.signals import anis_coefficients as ac

import sympy
from scipy.integrate import trapz

import scipy.linalg as sl

from . import clebschGordan as CG, utils

from scipy.interpolate import interp1d
from astroML.linear_model import LinearRegression

import lmfit
from lmfit import minimize, Parameters

class anis_pta():
    """A class to perform anisotropic GW searches using PTA data.

    This class can be used to perform anisotropic GW searches using PTA data by supplying
    it with the outputs of the PTA optimal statistic which can be found in
    (enterprise_extensions.frequentist.optimal_statistic or defiant/optimal_statistic).
    While you can include the OS values upon construction (xi, rho, sig, os),
    you can also use the method set_os_data() to set these values after construction.

    Attributes:
        psrs_theta (np.ndarray): An array of pulsar position theta [npsr].
        psrs_phi (np.ndarray): An array of pulsar position phi [npsr].
        npsr (int): The number of pulsars in the PTA.
        npair (int): The number of pulsar pairs.
        pair_idx (np.ndarray): An array of pulsar indices for each pair [npair x 2].
        xi (np.ndarray, optional): A list of pulsar pair separations from the OS [npair].
        rho (np.ndarray, optional): A list of pulsar pair correlations [npair].
            NOTE: rho is normalized by the OS value, making this slightly different from 
            what the OS uses. (i.e. OS calculates \hat{A}^2 * ORF while this uses ORF).
        sig (np.ndarray, optional): A list of 1-sigma uncertainties on rho [npair].
        os (float, optional): The OS' fit A^2 value.
        pair_cov (np.ndarray, optional): The pair covariance matrix [npair x npair].
        l_max (int): The maximum l value for spherical harmonics.
        nside (int): The nside of the healpix sky pixelization.
        npix (int): The number of pixels in the healpix pixelization.
        blmax (int): ???
        clm_size (int): The number of spherical harmonic modes.
        blm_size (int): The number of spherical harmonic modes for the sqrt power basis.
        gw_theta (np.ndarray): An array of source GW theta positions [npix].
        gw_phi (np.ndarray): An array of source GW phi positions [npix].
        use_physical_prior (bool): Whether to use physical priors or not.
        include_pta_monopole (bool): Whether to include the monopole term in the search.
        mode (str): The mode of the spherical harmonic decomposition to use.
            Must be 'power_basis', 'sqrt_power_basis', or 'hybrid'.
        sqrt_basis_helper (CG.clebschGordan): A helper object for the sqrt power basis.
        ndim (int): The number of dimensions for the search.
        F_mat (np.ndarray): The antenna response matrix [npair x npix].
        Gamma_lm (np.ndarray): The spherical harmonic basis [npair x ndim].
    """

    def __init__(self, psrs_theta, psrs_phi, xi = None, rho = None, sig = None, 
                 os = None, pair_cov = None, l_max = 6, nside = 2, mode = 'power_basis', 
                 use_physical_prior = False, include_pta_monopole = False, 
                 pair_idx = None):
        """Constructor for the anis_pta class.

        This function will construct an instance of the anis_pta class. This class
        can be used to perform anisotropic GW searches using PTA data by supplying
        it with the outputs of the PTA optimal statistic which can be found in 
        (enterprise_extensions.frequentist.optimal_statistic or defiant/optimal_statistic).
        While you can include the OS values upon construction (xi, rho, sig, os),
        you can also use the method set_os_data() to set these values after construction.
        
        Args:

        """
        # Pulsar positions
        self.psrs_theta = psrs_theta if type(psrs_theta) is np.ndarray else np.array(psrs_theta)
        self.psrs_phi = psrs_phi if type(psrs_phi) is np.ndarray else np.array(psrs_phi)
        if len(psrs_theta) != len(psrs_phi):
            raise ValueError("Pulsar theta and phi arrays must have the same length")
        self.npsr = len(psrs_theta)
        self.npairs = int( (self.npsr * (self.npsr - 1)) / 2)

        # OS values
        if pair_idx is None:
            self.pair_idx = np.array([(a,b) for a in range(self.npsr) for b in range(a+1,self.npsr)])
        else:
            self.pair_idx = pair_idx
        
        if xi is not None:
            if type(xi) is not np.ndarray:
                self.xi = np.array(xi)
            else:
                self.xi = xi
        else:
            self.xi = self._get_xi()
        self.rho, self.sig, self.os, self.pair_cov = None, None, None, None
        self.set_os_data(rho, sig, os, pair_cov)
        
        # Check if pair_idx is valid
        if len(self.pair_idx) != self.npairs:
            raise ValueError("pair_idx must have length equal to the number of pulsar pairs")
        
        # Pixel decomposition and Spherical harmonic parameters
        self.l_max = int(l_max)
        self.nside = int(nside)
        self.npix = hp.nside2npix(self.nside)

        # Some configuration for spherical harmonic basis runs
        # clm refers to normal spherical harmonic basis
        # blm refers to sqrt power spherical harmonic basis
        self.blmax = int(self.l_max / 2.)
        self.clm_size = (self.l_max + 1) ** 2
        self.blm_size = hp.Alm.getsize(self.blmax)

        self.gw_theta, self.gw_phi = hp.pix2ang(nside=self.nside, ipix=np.arange(self.npix))
       
        self.use_physical_prior = bool(use_physical_prior)
        self.include_pta_monopole = bool(include_pta_monopole)

        if mode in ['power_basis', 'sqrt_power_basis', 'hybrid']:
            self.mode = mode
        else:
            raise ValueError("mode must be either 'power_basis','sqrt_power_basis' or 'hybrid'")

        
        self.sqrt_basis_helper = CG.clebschGordan(l_max = self.l_max)
        #self.reorder, self.neg_idx, self.zero_idx, self.pos_idx = self.reorder_hp_ylm()

        if self.mode == 'power_basis':
            self.ndim = 1 + (self.l_max + 1) ** 2
        elif self.mode == 'sqrt_power_basis':
            #self.ndim = 1 + (2 * (hp.Alm.getsize(int(self.blmax)) - self.blmax))
            self.ndim = 1 + (self.blmax + 1) ** 2

        self.F_mat = self.antenna_response()

        if self.mode == 'power_basis' or self.mode == 'sqrt_power_basis':

            # The spherical harmonic basis for \Gamma_lm_mat shape (nclm, npsrs, npsrs)
            Gamma_lm_mat = ac.anis_basis(np.dstack((self.psrs_phi, self.psrs_theta))[0], 
                                         lmax = self.l_max, nside = self.nside)
            
            # We need to reorder Gamma_lm_mat to shape (nclm, npairs)
            self.Gamma_lm = np.zeros((Gamma_lm_mat.shape[0], self.npairs))
            for i, (a, b) in enumerate(self.pair_idx):
                self.Gamma_lm[:, i] = Gamma_lm_mat[:, a, b]

        return None
    
    
    def set_os_data(self, rho=None, sig=None, os=None, pair_cov=None):
        """Set the OS data for the anis_pta object.

        This function allows you to set the OS data for the anis_pta object 
        after construction. This allows users to use the same anis_pta object
        with different OS data from noise marginalization or per-frequency 
        analysis. xi is not required and if excluded will be calculated from pulsar
        positions. This function will normalize the rho, sig, and pair_cov by the 
        OS (A^2) value, making self.rho, self.sig, and self.pair_cov represent only 
        the correlations.

        Args:
            rho (list, optional): A list of pulsar pair correlated amplitudes (<rho> = <A^2*ORF>).
            sig (list, optional): A list of 1-sigma uncertaintties on rho.
            os (float, optional): The OS' fit A^2 value.
            pair_cov (np.ndarray, optional): The pair covariance matrix [npair x npair].
        """
        # Read in OS and normalize cross-correlations by OS. 
        # (i.e. get <rho/OS> = <ORF>)

        if (rho is not None) and (sig is not None) and (os is not None):
            self.os = os
            self.rho = np.array(rho) / self.os
            self.sig = np.array(sig) / self.os
        else:
            self.rho = None
            self.sig = None
            self.os = None

        if pair_cov is not None:
            self.pair_cov = pair_cov / self.os**2
        else:
            self.pair_cov = None
        
        # Set Covariance matrix inverse
        if self.sig is not None:
            self.N_mat_inv = self._get_N_mat_inv()


    def _get_N_mat_inv(self, ret_cond = False):
        """A method to calculate the inverse of the pair covariance matrix N.

        This function will calculate the inverse of the pair covariance matrix N.
        If the pair covariance matrix is not supplied, it will use a diagonal matrix
        consisting of the squared sig values. This function will use the woodbury
        identity to calculate the inverse of N if the pair covariance matrix is supplied,
        increasing the stability of the inverse.

        Args:
            ret_cond (bool, optional): A flag to return the condition number of the 
                covariance matrix. Only useful when using pair covariance.

        Returns:
            np.ndarray or tuple: The inverse of the pair covariance matrix N. If ret_cond
                is True, it will return a tuple containing the inverse and the condition
                number of the covariance matrix.
        """
        if self.pair_cov is not None:
            A = np.diag(self.sig ** 2)
            K = self.pair_cov - A
            In = np.eye(A.shape[0])
            if ret_cond:
                N_mat_inv, cond = utils.woodbury_inverse(A, In, In, K, ret_cond = True)
                return N_mat_inv, cond
            
            N_mat_inv = utils.woodbury_inverse(A, In, In, K)
        else:
            N_mat_inv = np.diag( 1 / self.sig ** 2 )

        return N_mat_inv
        

    def _get_radec(self):
        """Get the pulsar positions in RA and DEC."""
        psr_ra = self.psrs_phi
        psr_dec = (np.pi/2) - self.psrs_theta
        return psr_ra, psr_dec


    def _get_xi(self):
        """Calculate the angular separation between pulsar pairs.

        A function to compute the angular separation between pulsar pairs. This
        function will use a pair_idx array which is assigned upon construction 
        which ensures that the ordering of the pairs is consistent with the OS.

        Returns:
            np.ndarray: An array of pair separations.
        """
        psrs_ra, psrs_dec = self._get_radec()

        x = np.cos(psrs_ra)*np.cos(psrs_dec)
        y = np.sin(psrs_ra)*np.cos(psrs_dec)
        z = np.sin(psrs_dec)
        
        pos_vectors = np.array([x,y,z])

        a,b = self.pair_idx[:,0], self.pair_idx[:,1]

        xi = np.zeros( len(a) )
        # This effectively does a dot product of pulsar position vectors for all pairs a,b
        pos_dot = np.einsum('ij,ij->j', pos_vectors[:,a], pos_vectors[:, b])
        xi = np.arccos( pos_dot )

        return np.squeeze(xi)
    

    def _fplus_fcross(self, psrtheta, psrphi, gwtheta, gwphi):
        """Compute the antenna pattern functions. for each pulsar.

        This function comes primarily from NX01 (A propotype NANOGrav analysis 
        pipeline). This function supports vectorization for multiple pulsar positions 
        and multiple gw positions. 
        NOTE: This function uses the GW propogation direction for gwtheta and gwphi
        rather than the source direction (i.e. this method uses the vector from the
        source to the observer)
        
        Args:
            psrtheta (np.ndarray): An array of pulsar theta positions.
            psrphi (np.ndarray): An array of pulsar phi positions.
            gwtheta (np.ndarray): An array of GW theta propogation directions.
            gwphi (np.ndarray): An array of GW phi propogation directions.
        
        Returns:
            tuple: A tuple of two arrays, (Fplus, Fcross) containing the antenna 
                pattern functions for each pulsar.
        """
        # define variable for later use
        cosgwtheta, cosgwphi = np.cos(gwtheta), np.cos(gwphi)
        singwtheta, singwphi = np.sin(gwtheta), np.sin(gwphi)

        # unit vectors to GW source
        m = np.array([singwphi, -cosgwphi, 0.0])
        n = np.array([-cosgwtheta*cosgwphi, -cosgwtheta*singwphi, singwtheta])
        omhat = np.array([-singwtheta*cosgwphi, -singwtheta*singwphi, -cosgwtheta])

        # pulsar location
        ptheta = psrtheta
        pphi = psrphi

        # use definition from Sesana et al 2010 and Ellis et al 2012
        phat = np.array([np.sin(ptheta)*np.cos(pphi), np.sin(ptheta)*np.sin(pphi),\
                np.cos(ptheta)])

        fplus = 0.5 * (np.dot(m, phat)**2 - np.dot(n, phat)**2) / (1 - np.dot(omhat, phat))
        fcross = (np.dot(m, phat)*np.dot(n, phat)) / (1 - np.dot(omhat, phat))

        return fplus, fcross
    

    def antenna_response(self):
        """A function to compute the antenna response matrix R_{ab,k}.

        This function computes the antenna response matrix R_{ab,k} where ab 
        represents the pulsar pair made of pulsars a and b, and k represents
        the pixel index. 
        NOTE: This function uses the GW propogation direction for gwtheta and gwphi
        rather than the source direction (i.e. this method uses the vector from the
        source to the observer)

        Returns:
            np.ndarray: An array of shape (npairs, npix) containing the antenna
                pattern response matrix.
        """
        F_mat = np.zeros((self.npairs, self.npix))

        for ii,(a,b) in enumerate(self.pair_idx):
            for kk in range(self.npix):

                pp_1, pc_1 = self._fplus_fcross(self.psrs_theta[a], self.psrs_phi[a],
                                            self.gw_theta[kk], self.gw_phi[kk])
                
                pp_2, pc_2 = self._fplus_fcross(self.psrs_theta[b], self.psrs_phi[b],
                                            self.gw_theta[kk], self.gw_phi[kk])

                F_mat[ii][kk] =  (pp_1 * pp_2 + pc_1 * pc_2)  * 1.5 / (self.npix)

        return F_mat
    

    def get_pure_HD(self):
        """Calculate the Hellings and Downs correlation for each pulsar pair.

        This function calculates the Hellings and Downs correlation for each pulsar
        pair. This is done by using the values of xi potentially supplied upon 
        construction. 

        Returns:
            np.ndarray: An array of HD correlation values for each pulsar pair.
        """
        #Return the theoretical HD curve given xi

        xx = (1 - np.cos(self.xi)) / 2.
        hd_curve = 1.5 * xx * np.log(xx) - xx / 4 + 0.5

        return hd_curve
    

    def orf_from_clm(self, params):
        """A function to calculate the ORF from the clm values.

        This function calculates the ORF from the supplied clm values in params.
        params[0] indicates the monopole, params[1] indicates C_{1,-1} and so on.
        From there this function calculates the ORF for each pair given those
        clm values.

        Args:
            params (np.ndarray): An array of clm values.

        Returns:
            np.ndarray: An array of ORF values for each pulsar pair.
        """
        # Using supplied clm values, calculate the corresponding power map
        # and calculate the ORF from that power map (convoluted, I know)

        amp2 = 10 ** params[0]
        clm = params[1:]

        sh_map = ac.mapFromClm(clm, nside = self.nside)

        orf = amp2 * np.dot(self.F_mat, sh_map)

        return np.longdouble(orf)
    

    def clmFromAlm(self, alm):
        """A function to convert alm values to clm values.

        Given an array of clm values, return an array of complex alm valuex
        NOTE: There is a bug in healpy for the negative m values. This function
        just takes the imaginary part of the abs(m) alm index.

        Args:
            alm (np.ndarray): An array of alm values.

        Returns:
            np.ndarray: An array of clm values.
        """
        #nalm = len(alm)
        #maxl = int(np.sqrt(9.0 - 4.0 * (2.0 - 2.0 * nalm)) * 0.5 - 1.5)  # Really?
        maxl = self.l_max
        nclm = (maxl + 1) ** 2

        # Check the solution. Went wrong one time..
        #if nalm != int(0.5 * (maxl + 1) * (maxl + 2)):
        #    raise ValueError("Check numerical precision. This should not happen")

        clm = np.zeros(nclm)

        clmindex = 0
        for ll in range(0, maxl + 1):
            for mm in range(-ll, ll + 1):
                almindex = hp.Alm.getidx(maxl, ll, abs(mm))

                if mm == 0:
                    clm[clmindex] = alm[almindex].real
                elif mm < 0:
                    clm[clmindex] = (-1) ** mm * alm[almindex].imag * np.sqrt(2)
                elif mm > 0:
                    clm[clmindex] = (-1) ** mm * alm[almindex].real * np.sqrt(2)

                clmindex += 1

        return clm
    

    def fisher_matrix_sph(self):
        """A method to calculate the Fisher matrix for the spherical harmonic basis.

        A method which calculates the fisher matrix for the spherical harmonic basis.
        This method will use pair covariance if self.pair_cov is not None, otherwise
        it will use a diagonal matrix with the self.sig values.

        Returns:
            np.array: The Fisher matrix for the spherical harmonic basis. [n_clm x n_clm]
        """
        if self.pair_cov is not None:
            A = np.diag(self.sig ** 2)
            K = self.pair_cov - A
            In = np.eye(A.shape[0])
            N_mat_inv = utils.woodbury_inverse(A, In, In, K)
        else:
            N_mat_inv = np.diag( 1 / self.sig ** 2 )

        F_mat_clm = self.Gamma_lm.transpose()

        fisher_mat = F_mat_clm.T @ N_mat_inv @ F_mat_clm 

        return fisher_mat


    def fisher_matrix_pixel(self):
        """A method to calculate the Fisher matrix for the pixel basis.

        A method which calculates the fisher matrix for the pixel basis. If self.pair_cov
        is not None, it will use the pair covariance matrix, otherwise it will use a
        diagonal matrix with the self.sig values.

        Returns:
            np.ndarray: The Fisher matrix for the pixel basis. [npix x npix]
        """

        if self.pair_cov is not None:
            A = np.diag(self.sig ** 2)
            K = self.pair_cov - A
            In = np.eye(A.shape[0])
            N_mat_inv = utils.woodbury_inverse(A, In, In, K)
        else:
            N_mat_inv = np.diag( 1 / self.sig ** 2 )

        fisher_mat = self.F_mat.T @ N_mat_inv @ self.F_mat
        return fisher_mat
    

    def max_lkl_pixel(self, cutoff = 0, return_fac1 = False, use_svd_reg = False, 
                      reg_type = 'l2', alpha = 0):
        """A method to calculate the maximum likelihood pixel values.

        This method calculates the maximum likelihood pixel values while allowing
        for covariance between pixels. This method is similar to that of the
        get_radiometer_map method, but allows for covariance between pixels, and 
        use regression to find solutions through forward modeling.

        Args:
            cutoff (int, optional): _description_. Defaults to 0.
            return_fac1 (bool, optional): _description_. Defaults to False.
            use_svd_reg (bool, optional): _description_. Defaults to False.
            reg_type (str, optional): _description_. Defaults to 'l2'.
            alpha (int, optional): _description_. Defaults to 0.

        Returns:
            tuple: _description_
        """
        FNF = self.F_mat.T @ self.N_mat_inv @ self.F_mat 
        sv = sl.svd(FNF, compute_uv = False,)

        cn = np.max(sv) / np.min(sv)

        if use_svd_reg:
            abs_cutoff = cutoff * np.max(sv)
            fac1 = sl.pinvh( FNF, cond = abs_cutoff )
            fac2 = self.F_mat.T @ self.N_mat_inv @ self.rho
            pow_err = np.sqrt(np.diag(fac1))
            power = fac1 @ fac2

        else:
            diag_identity = np.diag(np.full(self.F_mat.shape[1], 1))
            fac1r = sl.pinvh(FNF + alpha * diag_identity)
            pow_err = np.sqrt(np.diag(fac1r))
            clf = LinearRegression(regularization = reg_type, fit_intercept = False, 
                                   kwds = dict(alpha = alpha))
            
            if self.pair_cov is not None:
                clf.fit(self.F_mat, self.rho, self.pair_cov)
            else:
                clf.fit(self.F_mat, self.rho, self.sig)
            power = clf.coef_

        if return_fac1:
            return power, pow_err, cn, sv, fac1
        return power, pow_err, cn, sv
    

    def get_radiometer_map(self):
        """A method to get the radiometer pixel map.

        This method calculates the radiometer pixel map for all pixels. This method
        calculates power per pixel assuming there is no power in other pixels.
        NOTE: This function uses the GW propogation direction for each pixel
        rather than the source direction (i.e. this method uses the vector from the
        source to the observer). Use utils.invert_omega() to get the source direction
        instead!

        Returns:
            tuple: A tuple of 2 np.ndarrays containing the pixel power map and the
                pixel power map error.
        """
        if self.pair_cov is not None:
            A = np.diag(self.sig ** 2)
            K = self.pair_cov - A
            In = np.eye(A.shape[0])
            N_mat_inv = utils.woodbury_inverse(A, In, In, K)
        else:
            N_mat_inv = np.diag( 1 / self.sig ** 2 )
    
        dirty_map = self.F_mat.T @ N_mat_inv @ self.rho
    
        # Calculate radiometer map, (i.e. no covariance between pixels)
        # If you take only the diagonal elements of the fisher matrix, 
        # you assume there is no covariance between pixels, so it will calculate
        # the power in each pixel assuming no power in others.

        # Get the full fisher matrix
        fisher_mat = self.fisher_matrix_pixel()
        # Get only the diagonal elements and invert
        fisher_diag_inv = np.diag( 1/np.diag(fisher_mat) )
    
        # Solve
        radio_map = fisher_diag_inv @ dirty_map

        # The error needs to be normalized by the area of the pixel
        pix_area = hp.nside2pixarea(nside = self.nside)
        radio_map_n = radio_map * 4 * np.pi / trapz(radio_map, dx = pix_area)
    
        radio_map_err = np.sqrt(np.diag(fisher_diag_inv)) * 4 * np.pi / trapz(radio_map, dx = pix_area)
    
        return radio_map_n, radio_map_err


    def max_lkl_clm(self, cutoff = 0, use_svd_reg = False, reg_type = 'l2', alpha = 0):
        """Compute the max likelihood clm values.

        A method to compute the maximum likelihood clm values. This method uses
        the Fisher matrix for the spherical harmonic basis to calculate the
        maximum likelihood clm values. This method allows for the use of SVD
        regularization or other regularization methods found in 
        astroML.linear_model.LinearRegression if use_svd_reg is False. If 
        use_svd_reg is True, it will solve the linear system using the SVD, otherwise
        it will use the LinearRegression to solve the system using forward modeling.

        Args:
            cutoff (float): The minimum allowed singular value for the SVD.
            use_svd_reg (bool): A flag to use linear solving with SVD regularization.
            reg_type (str, optional): The type of regularization to use with LinearRegression. 
                Defaults to 'l2'.
            alpha (float, optional): Optional jitter to add to the diagonal of the Fisher matrix
                when using LinearRegression. Defaults to 0.

        Returns:
            tuple: A tuple of 4 np.ndarrays containing the clm values, the clm value errors,
                the condition number of the Fisher matrix, and the singular values of the Fisher matrix.
        """
        F_mat_clm = self.Gamma_lm.T

        FNF_clm = F_mat_clm.T @ self.N_mat_inv @ F_mat_clm
        sv = sl.svd( FNF_clm, compute_uv = False)

        cn = np.max(sv) / np.min(sv)

        if use_svd_reg:
            abs_cutoff = cutoff * np.max(sv)

            fac1 = sl.pinvh(FNF_clm, cond = abs_cutoff)
            fac2 = F_mat_clm.T @ self.N_mat_inv @ self.rho

            clms = fac1 @ fac2
            clm_err = np.sqrt(np.diag(fac1))

        else:
            diag_identity = np.diag(np.ones(self.clm_size))

            fac1r = sl.pinvh(FNF_clm + alpha * diag_identity)

            clf = LinearRegression(regularization = reg_type, fit_intercept = False, kwds = dict(alpha = alpha))

            if self.pair_cov is not None:
                clf.fit(F_mat_clm, self.rho, self.pair_cov)
            else:
                clf.fit(F_mat_clm, self.rho, self.sig)

            clms = clf.coef_

            clm_err = np.sqrt(np.diag(fac1r))

        return clms, clm_err, cn, sv


    def setup_lmfit_parameters(self):
        """A method to setup the lmfit Parameters object for the search.

        NOTE: This method is designed specifcally for the sqrt power basis. As
        such, it will not work for the power basis and may throw exceptions!

        TODO: Make this function work for other bases!

        Returns:
            lmfit.Parameters: The lmfit Parameters object
        """
        # This shape is to fit with lmfit's Parameter.add_many();
        # Format is (name, value, vary, min, max, expr, brute_step)
        params = []

        if self.include_pta_monopole:
            # (name, value, vary, min, max, expr, brute_step)
            x = ['A_mono', np.log10(nr.uniform(1e-2, 3)), True, np.log10(1e-2), np.log10(1e2), None, None]
            params.append(x)

            x = ['A2', np.log10(nr.uniform(1e-2, 3)), True, np.log10(1e-2), np.log10(1e2), None, None]
            params.append(x)
        else:
            x = ['A2', np.log10(nr.uniform(0, 3)), True, np.log10(1e-2), np.log10(1e2), None, None]
            params.append(x)

        # Now for non-monopole terms!
        for ll in range(self.blmax + 1):
            for mm in range(0, ll + 1):

                if ll == 0:
                    # (name, value, vary, min, max, expr, brute_step)
                    x = ['b_{}{}'.format(ll, mm), 1., False, None, None, None, None]
                    params.append(x)
                    
                elif mm == 0:
                    # (name, value, vary, min, max, expr, brute_step)
                    x = ['b_{}{}'.format(ll, mm), nr.uniform(-1, 1), True, None, None, None, None]
                    params.append(x)

                elif mm != 0:
                    # (name, value, vary, min, max, expr, brute_step)
                    #Amplitude is always >= 0; initial guess set to small non-zero value
                    x = ['b_{}{}_amp'.format(ll, mm), nr.uniform(0, 3), True, 0, None, None, None]
                    params.append(x)
                    
                    x = ['b_{}{}_phase'.format(ll, mm), nr.uniform(0, 2 * np.pi), True, 0, 2 * np.pi, None, None]
                    params.append(x)

        lmf_params = Parameters()
        lmf_params.add_many(*params)

        return lmf_params
    

    def max_lkl_sqrt_power(self, params = np.array(())):
        """A method to calculate the maximum likelihood b_lms for the sqrt power basis.

        This method uses lmfit to minimize the chi-square to find the maximum likelihood
        b_lms for the sqrt power basis. Post-processing help can be found in the utils 
        and lmfit documentation.

        Args:
            params (np.ndarray, optional): ???. Defaults to an empty array.

        Returns:
            lmfit.Minimizer.minimize: The lmfit minimizer object for post-processing.
        """

        if len(params) == 0:
            params = self.setup_lmfit_parameters()
        else:
            params = params

        def residuals(params, obs_orf, obs_orf_err):

            #Get the input parameters as a dictionary
            param_dict = params.valuesdict()

            #Convert the values to a numpy array for using in other pta_anis functions
            param_arr = np.array(list(param_dict.values()))

            #Do the thing
            if self.include_pta_monopole:
                clm_pred = utils.convert_blm_params_to_clm(self, param_arr[2:])
            else:
                clm_pred = utils.convert_blm_params_to_clm(self, param_arr[1:])

            if self.include_pta_monopole:
                sim_orf = (10 ** param_arr[1]) * np.sum(clm_pred[:, np.newaxis] * self.Gamma_lm, axis = 0) + (10 ** param_arr[0]) * 1
            else:
                sim_orf = (10 ** param_arr[0]) * np.sum(clm_pred[:, np.newaxis] * self.Gamma_lm, axis = 0)

            return (sim_orf - obs_orf) / obs_orf_err

        #Get initial fit from function above. Includes initial guess
        #init_guess = self.setup_lmfit_parameters()

        #Setup lmfit minimizer and get solution
        mini = lmfit.Minimizer(residuals, params, fcn_args=(self.rho, self.sig))
        opt_params = mini.minimize()

        #Return the full output object for user.
        #Post-processing help in utils and lmfit documentation
        return opt_params


    def prior(self, params):
        """A method to calculate the prior for power and sqrt power bases for the given parameters.

        This method returns the prior given a parameter values for the clm (for
        the power bases) or the blm (for the sqrt power bases). This method also
        takes into acount whether you enable the physical prior or not.

        Args:
            params (np.ndarray or list): An array of parameters to calculate the prior for.

        Returns:
            float: The (non-log) prior value for the given parameters.
        """

        if self.mode == 'power_basis':
            amp2 = params[0]
            clm = params[1:]
            maxl = int(np.sqrt(len(clm)))

            if clm[0] != np.sqrt(4 * np.pi) or any(np.abs(clm[1:]) > 15) or (amp2 < np.log10(1e-5) or amp2 > np.log10(1e3)):
                return -np.inf #0 #np.longdouble(1e-300)
            elif self.use_physical_prior:
                #NOTE: if using physical prior, make sure to set initial sample to isotropy

                sh_map = ac.mapFromClm(clm, nside = self.nside)

                if np.any(sh_map < 0):
                    return -np.inf #0
                else:
                    return np.longdouble((1 / 10) ** (len(params) - 1))

            else:
                return np.longdouble((1 / 10) ** (len(params) - 1))

        elif self.mode == 'sqrt_power_basis':
            amp2 = params[0]
            blm_params = params[1:]

            if (amp2 < np.log10(1e-5) or amp2 > np.log10(1e3)):
                return -np.inf #0

            else:
                idx = 1
                for ll in range(self.blmax + 1):
                    for mm in range(0, ll + 1):

                        if ll == 0 and params[idx] != 1:
                            return -np.inf #0

                        if mm == 0 and (params[idx] > 5 or params[idx] < -5):
                            return -np.inf #0

                        if mm != 0 and (params[idx] > 5 or params[idx] < 0 or params[idx + 1] >= 2 * np.pi or params[idx + 1] < 0):
                            return -np.inf #0

                        if ll == 0:
                            idx += 1
                        elif mm == 0:
                            idx += 1
                        else:
                            idx += 2

                return np.longdouble((1 / 10) ** (len(params) - 1))


    def logLikelihood(self, params):

        if self.mode == 'hybrid':

            amp2 = params[0]
            clm = params[1:]

            #Fix c_00 to sqrt(4 * pi)
            clm[0] = np.sqrt(4 * np.pi)

            sim_orf = self.orf_from_clm(params)

            #Decompose the likelihood which is a product of gaussians into sums when getting log-likely
            alpha = (self.rho - sim_orf) ** 2 / (2 * self.sig ** 2)
            beta = np.longdouble(1 / (self.sig * np.sqrt(2 * np.pi)))

            loglike = np.sum(np.log(beta)) - np.sum(alpha)

        elif self.mode == 'power_basis':

            amp2 = 10 ** params[0]
            clm = params[1:]

            #clm[0] = np.sqrt(4 * np.pi)

            sim_orf = amp2 * np.sum(clm[:, np.newaxis] * self.Gamma_lm, axis = 0)

            alpha = (self.rho - sim_orf) ** 2 / (2 * self.sig ** 2)
            beta = np.longdouble(1 / (self.sig * np.sqrt(2 * np.pi)))

            loglike = np.sum(np.log(beta)) - np.sum(alpha)

        elif self.mode == 'sqrt_power_basis':

            amp2 = 10 ** params[0]
            blm_params = params[1:]

            #blm_params[0] = 1

            #b00 is set internally, no need to pass it in
            blm = self.sqrt_basis_helper.blm_params_2_blms(blm_params[1:])

            new_clms = self.sqrt_basis_helper.blm_2_alm(blm)

            #Only need m >= 0 entries for clmfromalm
            clms_rvylm = self.clmFromAlm(new_clms)
            #clms_rvylm[0] *= 6.283185346689728

            sim_orf = amp2 * np.sum(clms_rvylm[:, np.newaxis] * self.Gamma_lm, axis = 0)

            alpha = (self.rho - sim_orf) ** 2 / (2 * self.sig ** 2)
            beta = np.longdouble(1 / (self.sig * np.sqrt(2 * np.pi)))

            loglike = np.sum(np.log(beta)) - np.sum(alpha)

        return loglike
        #return np.prod(gauss, dtype = np.longdouble)

    def logPrior(self, params):
        """A method to calculate the log of the prior for the given parameters.

        NOTE: This function simply returns the np.log() for the prior() function.

        Args:
            params (np.ndarray or list): An array of parameters to calculate the prior for.

        Returns:
            float: The log_10 of the prior value for the given parameters.
        """
        return np.log(self.prior(params))

    def get_random_sample(self):

        if self.mode == 'power_basis':

            if self.use_physical_prior:
                x0 = np.append(np.append(np.log10(nr.uniform(0, 30, 1)), np.array([np.sqrt(4 * np.pi)])), np.repeat(0, self.ndim - 2))
            else:
                x0 = np.append(np.append(np.log10(nr.uniform(0, 30, 1)), np.array([np.sqrt(4 * np.pi)])), nr.uniform(-5, 5, self.ndim - 2))

        elif self.mode == 'sqrt_power_basis':

            x0 = np.full(self.ndim, 0.0)

            x0[0] = nr.uniform(np.log10(1e-5), np.log10(30))

            idx = 1
            for ll in range(self.blmax + 1):
                for mm in range(0, ll + 1):

                    if ll == 0:
                        x0[idx] = 1.
                        idx += 1

                    elif mm == 0:
                        x0[idx] = 0 #nr.uniform(0, 5)
                        idx += 1

                    elif mm != 0:
                        x0[idx] = 0 #nr.uniform(0, 5)
                        x0[idx + 1] = 0 #nr.uniform(0, 2 * np.pi)
                        idx += 2

        return x0

    def amplitude_scaling_factor(self):
        return 1 / (2 * 6.283185346689728)

class anis_hypermodel():

    def __init__(self, models, log_weights = None, mode = 'sqrt_power_basis', use_physical_prior = True):

        self.models = models
        self.num_models = len(self.models)
        self.mode = mode
        self.use_physical_prior = use_physical_prior

        if log_weights is None:
            self.log_weights = log_weights
        else:
            self.log_weights = np.longdouble(log_weights)

        self.nside = np.max([xx.nside for xx in self.models])

        self.ndim = np.max([xx.ndim for xx in self.models]) + 1

        self.l_max = np.max([xx.l_max for xx in self.models])

        #Some configuration for spherical harmonic basis runs
        #clm refers to normal spherical harmonic basis
        #blm refers to sqrt power spherical harmonic basis
        self.blmax = int(self.l_max / 2)
        self.clm_size = (self.l_max + 1) ** 2
        self.blm_size = hp.Alm.getsize(self.blmax)

    def _standard_prior(self, params):

        if self.mode == 'power_basis':
            amp2 = params[0]
            clm = params[1:]
            maxl = int(np.sqrt(len(clm)))

            if clm[0] != np.sqrt(4 * np.pi) or any(np.abs(clm[1:]) > 15) or (amp2 < np.log10(1e-5) or amp2 > np.log10(1e3)):
                return -np.inf
            elif self.use_physical_prior:
                #NOTE: if using physical prior, make sure to set initial sample to isotropy

                sh_map = ac.mapFromClm(clm, nside = self.nside)

                if np.any(sh_map < 0):
                    return -np.inf
                else:
                    return np.longdouble((1 / 10) ** (len(params) - 1))

            else:
                return np.longdouble((1 / 10) ** (len(params) - 1))

        elif self.mode == 'sqrt_power_basis':
            amp2 = params[0]
            blm_params = params[1:]

            if (amp2 < np.log10(1e-5) or amp2 > np.log10(1e3)):
                return -np.inf

            else:
                idx = 1
                for ll in range(self.blmax + 1):
                    for mm in range(0, ll + 1):

                        if ll == 0 and params[idx] != 1:
                            return -np.inf #0

                        if mm == 0 and (params[idx] > 5 or params[idx] < -5):
                            return -np.inf #0

                        if mm != 0 and (params[idx] > 5 or params[idx] < 0 or params[idx + 1] >= 2 * np.pi or params[idx + 1] < 0):
                            return -np.inf #0

                        if ll == 0:
                            idx += 1
                        elif mm == 0:
                            idx += 1
                        else:
                            idx += 2

                return np.longdouble((1 / 10) ** (len(params) - 1))

    def logPrior(self, params):

        nmodel = int(np.rint(params[0]))
        #print(nmodel)

        if nmodel <= -0.5 or nmodel > 0.5 * (2 * self.num_models - 1):
            return -np.inf

        return np.log(self._standard_prior(params[1:]))

    def logLikelihood(self, params):

        nmodel = int(np.rint(params[0]))
        #print(nmodel)

        active_lnlkl = self.models[nmodel].logLikelihood(params[1: self.models[nmodel].ndim + 1])

        if self.log_weights is not None:
            active_lnlkl += self.log_weights[nmodel]

        return active_lnlkl

    def get_random_sample(self):

        if self.mode == 'power_basis':

            if self.use_physical_prior:
                x0 = np.append(np.append(np.log10(nr.uniform(0, 30, 1)), np.array([np.sqrt(4 * np.pi)])), np.repeat(0, self.ndim - 3))
                x0 = np.append(nr.uniform(-0.5, 0.5 + self.num_models, 1), x0)
            else:
                x0 = np.append(np.append(np.log10(nr.uniform(0, 30, 1)), np.array([np.sqrt(4 * np.pi)])), nr.uniform(-5, 5, self.ndim - 3))
                x0 = np.append(nr.uniform(-0.5, 0.5 + self.num_models, 1), x0)

        elif self.mode == 'sqrt_power_basis':

            x0 = np.full(self.ndim, 0.0)

            x0[0] = nr.uniform(-0.5, 0.5 * (2 * self.num_models - 1), 1)

            x0[1] = nr.uniform(np.log10(1e-5), np.log10(30))

            idx = 2
            for ll in range(self.blmax + 1):
                for mm in range(0, ll + 1):

                    if ll == 0:
                        x0[idx] = 1.
                        idx += 1

                    elif mm == 0:
                        x0[idx] = 0 #nr.uniform(0, 5)
                        idx += 1

                    elif mm != 0:
                        x0[idx] = 0 #nr.uniform(0, 5)
                        x0[idx + 1] = 0 #nr.uniform(0, 2 * np.pi)
                        idx += 2

        return x0
