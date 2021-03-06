#
# LSST Data Management System
#
# Copyright 2008-2018  AURA/LSST.
#
# This product includes software developed by the
# LSST Project (http://www.lsst.org/).
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope hat it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the LSST License Statement and
# the GNU General Public License along with this program.  If not,
# see <https://www.lsstcorp.org/LegalNotices/>.
#

__all__ = ['ProcessStarTask', 'ProcessStarTaskConfig']

import os
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from importlib import reload

import astropy
from astroquery.simbad import Simbad
from astroquery.vizier import Vizier
import astropy.units as u
from astropy.coordinates import SkyCoord, Distance

import lsstDebug
import lsst.afw.image as afwImage
from lsst.afw.image.utils import defineFilter
import lsst.geom as geom
from lsst.ip.isr import IsrTask
import lsst.log as lsstLog
import lsst.pex.config as pexConfig
import lsst.pipe.base as pipeBase
from lsst.utils import getPackageDir
from lsst.pipe.tasks.characterizeImage import CharacterizeImageTask
from lsst.meas.algorithms import LoadIndexedReferenceObjectsTask, MagnitudeLimit
from lsst.meas.astrom import AstrometryTask, FitAffineWcsTask

import lsst.afw.detection as afwDetect
import lsst.afw.geom as afwGeom

from .spectraction import SpectractorShim

COMMISSIONING = False  # allows illegal things for on the mountain usage.


def getTargetCentroidFromWcs(exp, target, doMotionCorrection=True, logger=None):
    """Get the target's centroid, given an exposure with fitted WCS.

    Parameters
    ----------
    exp : `lsst.afw.exposure.Exposure`
        Exposure with fitted WCS.

    target : `str`
        The name of the target, e.g. 'HD 55852'

    doMotionCorrection : `bool`, optional
        Correct for proper motion and parallax if possible.
        This requires the object is found in Vizier rather than Simbad.
        If that is not possible, a warning is logged, and the uncorrected
        centroid is returned.

    Returns
    -------
    pixCoord : `tuple` of `float`, or `None`
        The pixel (x, y) of the target's centroid, or None if the object
        is not found.
    """
    if logger is None:
        logger = lsstLog.Log.getDefaultLogger()

    resultFrom = None
    targetLocation = None
    # try vizier, but it is slow, unreliable, and
    # many objects are found but have no Hipparcos entries
    try:
        targetLocation = vizierLocationForTarget(exp, target, doMotionCorrection=doMotionCorrection)
        resultFrom = 'vizier'
        logger.info(f"Target location for {target} retrieved from Vizier")

    # fail over to simbad - it has ~every target, but no proper motions
    except ValueError:
        try:
            logger.warn(f"Target {target} not found in Vizier, failing over to try Simbad")
            targetLocation = simbadLocationForTarget(target)
            resultFrom = 'simbad'
            logger.info(f"Target location for {target} retrieved from Simbad")
        except ValueError as inst:  # simbad found zero or several results for target
            msg = inst.args[0]
            logger.warn(msg)
            return None

    if not targetLocation:
        return None

    if doMotionCorrection and resultFrom == 'simbad':
        logger.warn(f"Failed to apply motion correction because {target} was"
                    " only found in Simbad, not Vizier/Hipparcos")

    pixCoord = exp.getWcs().skyToPixel(targetLocation)
    return pixCoord


def simbadLocationForTarget(target):
    """Get the target location from Simbad.

    Parameters
    ----------
    target : `str`
        The name of the target, e.g. 'HD 55852'

    Returns
    -------
    targetLocation : `lsst.geom.SpherePoint`
        Nominal location of the target object, uncorrected for
        proper motion and parallax.

    Raises
    ------
    ValueError
        If object not found, or if multiple entries for the object are found.
    """
    obj = Simbad.query_object(target)
    if not obj:
        raise ValueError(f"Found failed to find {target} in simbad!")
    if len(obj) != 1:
        raise ValueError(f"Found {len(obj)} simbad entries for {target}!")

    raStr = obj[0]['RA']
    decStr = obj[0]['DEC']
    skyLocation = SkyCoord(raStr, decStr, unit=(u.hourangle, u.degree), frame='icrs')
    raRad, decRad = skyLocation.ra.rad, skyLocation.dec.rad
    ra = geom.Angle(raRad)
    dec = geom.Angle(decRad)
    targetLocation = geom.SpherePoint(ra, dec)
    return targetLocation


def vizierLocationForTarget(exp, target, doMotionCorrection):
    """Get the target location from Vizier optionally correction motion.

    Parameters
    ----------
    target : `str`
        The name of the target, e.g. 'HD 55852'

    Returns
    -------
    targetLocation : `lsst.geom.SpherePoint` or `None`
        Location of the target object, optionally corrected for
        proper motion and parallax.

    Raises
    ------
    ValueError
        If object not found in Hipparcos2 via Vizier.
        This is quite common, even for bright objects.
    """

    result = Vizier.query_object(target)  # result is an empty table list for an unknown target
    try:
        star = result['I/311/hip2']
    except TypeError:  # if 'I/311/hip2' not in result (result doesn't allow easy checking without a try)
        raise ValueError

    epoch = "J1991.25"
    coord = SkyCoord(ra=star[0]['RArad']*u.Unit(star['RArad'].unit),
                     dec=star[0]['DErad']*u.Unit(star['DErad'].unit),
                     obstime=epoch,
                     pm_ra_cosdec=star[0]['pmRA']*u.Unit(star['pmRA'].unit),  # NB contains cosdec already
                     pm_dec=star[0]['pmDE']*u.Unit(star['pmDE'].unit),
                     distance=Distance(parallax=star[0]['Plx']*u.Unit(star['Plx'].unit)))

    if doMotionCorrection:
        expDate = exp.getInfo().getVisitInfo().getDate()
        obsTime = astropy.time.Time(expDate.get(expDate.EPOCH), format='jyear', scale='tai')
        newCoord = coord.apply_space_motion(new_obstime=obsTime)
    else:
        newCoord = coord

    raRad, decRad = newCoord.ra.rad, newCoord.dec.rad
    ra = geom.Angle(raRad)
    dec = geom.Angle(decRad)
    targetLocation = geom.SpherePoint(ra, dec)
    return targetLocation


class ProcessStarTaskConfig(pexConfig.Config):
    """Configuration parameters for ProcessStarTask."""

    isr = pexConfig.ConfigurableField(
        target=IsrTask,
        doc="Task to perform instrumental signature removal",
    )
    charImage = pexConfig.ConfigurableField(
        target=CharacterizeImageTask,
        doc="""Task to characterize a science exposure:
            - detect sources, usually at high S/N
            - estimate the background, which is subtracted from the image and returned as field "background"
            - estimate a PSF model, which is added to the exposure
            - interpolate over defects and cosmic rays, updating the image, variance and mask planes
            """,
    )
    doWrite = pexConfig.Field(
        dtype=bool,
        doc="Write out the results?",
        default=True,
    )
    mainSourceFindingMethod = pexConfig.ChoiceField(
        doc="Which attribute to prioritize when selecting the main source object",
        dtype=str,
        default="BRIGHTEST",
        allowed={
            "BRIGHTEST": "Select the brightest object with roundness > roundnessCut",
            "ROUNDEST": "Select the roundest object with brightness > fluxCut",
        }
    )
    mainStarRoundnessCut = pexConfig.Field(
        dtype=float,
        doc="Value of ellipticity above which to reject the brightest object."
        " Ignored if mainSourceFindingMethod == BRIGHTEST",
        default=0.2
    )
    mainStarFluxCut = pexConfig.Field(
        dtype=float,
        doc="Object flux below which to reject the roundest object."
        " Ignored if mainSourceFindingMethod == ROUNDEST",
        default=1e7
    )
    mainStarNpixMin = pexConfig.Field(
        dtype=int,
        doc="Minimum number of pixels for object detection of main star",
        default=10
    )
    mainStarNsigma = pexConfig.Field(
        dtype=int,
        doc="nSigma for detection of main star",
        default=200  # the m=0 is very bright indeed, and we don't want to detect much spectrum
    )
    mainStarGrow = pexConfig.Field(
        dtype=int,
        doc="Number of pixels to grow by when detecting main star. This"
        " encourages the spectrum to merge into one footprint, but too much"
        " makes everything round, compromising mainStarRoundnessCut's"
        " effectiveness",
        default=5
    )
    mainStarGrowIsotropic = pexConfig.Field(
        dtype=bool,
        doc="Grow main star's footprint isotropically?",
        default=False
    )
    aperture = pexConfig.Field(
        dtype=int,
        doc="Width of the aperture to use in pixels",
        default=250
    )
    spectrumLengthPixels = pexConfig.Field(
        dtype=int,
        doc="Length of the spectrum in pixels",
        default=5000
    )
    offsetFromMainStar = pexConfig.Field(
        dtype=int,
        doc="Number of pixels from the main star's centroid to start extraction",
        default=100
    )
    dispersionDirection = pexConfig.ChoiceField(
        doc="Direction along which the image is dispersed",
        dtype=str,
        default="y",
        allowed={
            "x": "Dispersion along the serial direction",
            "y": "Dispersion along the parallel direction",
        }
    )
    spectralOrder = pexConfig.ChoiceField(
        doc="Direction along which the image is dispersed",
        dtype=str,
        default="+1",
        allowed={
            "+1": "Use the m+1 spectrum",
            "-1": "Use the m-1 spectrum",
            "both": "Use both spectra",
        }
    )
    doFlat = pexConfig.Field(
        dtype=bool,
        doc="Flatfield the image?",
        default=True
    )
    doCosmics = pexConfig.Field(
        dtype=bool,
        doc="Repair cosmic rays?",
        default=True
    )
    forceObjectName = pexConfig.Field(
        dtype=str,
        doc="A supplementary name for OBJECT. Will be forced to apply to ALL visits, so this should only"
            " ONLY be used for immediate commissioning debug purposes. All long term fixes should be"
            " supplied as header fix-up yaml files.",
        default=""
    )
    referenceFilterOverride = pexConfig.Field(
        dtype=str,
        doc="Which filter in the reference catalog to match to?",
        default="phot_g_mean"
    )

    def setDefaults(self):
        self.isr.doWrite = False
        self.charImage.doWriteExposure = False

        self.charImage.doApCorr = False
        self.charImage.doMeasurePsf = False
        # if self.charImage.doMeasurePsf:
        #     self.charImage.measurePsf.starSelector['objectSize'].signalToNoiseMin = 10.0
        #     self.charImage.measurePsf.starSelector['objectSize'].fluxMin = 5000.0
        self.charImage.detection.includeThresholdMultiplier = 3
        self.isr.overscan.fitType = 'MEDIAN_PER_ROW'


class ProcessStarTask(pipeBase.CmdLineTask):
    """Task for the spectral extraction of single-star dispersed images.

    For a full description of how this tasks works, see the run() method.
    """

    ConfigClass = ProcessStarTaskConfig
    RunnerClass = pipeBase.ButlerInitializedTaskRunner
    _DefaultName = "processStar"

    def __init__(self, *, butler=None, psfRefObjLoader=None, **kwargs):
        # TODO: rename psfRefObjLoader to refObjLoader
        super().__init__(**kwargs)
        self.makeSubtask("isr")
        self.makeSubtask("charImage", butler=butler, refObjLoader=psfRefObjLoader)

        self.debug = lsstDebug.Info(__name__)
        if self.debug.enabled:
            self.log.info("Running with debug enabled...")
            # If we're displaying, test it works and save displays for later.
            # It's worth testing here as displays are flaky and sometimes
            # can't be contacted, and given processing takes a while,
            # it's a shame to fail late due to display issues.
            if self.debug.display:
                try:
                    import lsst.afw.display as afwDisp
                    afwDisp.setDefaultBackend(self.debug.displayBackend)
                    afwDisp.Display.delAllDisplays()
                    # pick an unlikely number to be safe xxx replace this
                    self.disp1 = afwDisp.Display(987, open=True)

                    im = afwImage.ImageF(2, 2)
                    im.array[:] = np.ones((2, 2))
                    self.disp1.mtv(im)
                    self.disp1.erase()
                    afwDisp.setDefaultMaskTransparency(90)
                except NameError:
                    self.debug.display = False
                    self.log.warn('Failed to setup/connect to display! Debug display has been disabled')

        if self.debug.notHeadless:
            pass  # other backend options can go here
        else:  # this stop windows popping up when plotting. When headless, use 'agg' backend too
            plt.interactive(False)

        self.config.validate()
        self.config.freeze()

    def findObjects(self, exp, nSigma=None, grow=0):
        """Find the objects in a postISR exposure."""
        nPixMin = self.config.mainStarNpixMin
        if not nSigma:
            nSigma = self.config.mainStarNsigma
        if not grow:
            grow = self.config.mainStarGrow
            isotropic = self.config.mainStarGrowIsotropic

        threshold = afwDetect.Threshold(nSigma, afwDetect.Threshold.STDEV)
        footPrintSet = afwDetect.FootprintSet(exp.getMaskedImage(), threshold, "DETECTED", nPixMin)
        if grow > 0:
            footPrintSet = afwDetect.FootprintSet(footPrintSet, grow, isotropic)
        return footPrintSet

    def _getEllipticity(self, shape):
        """Calculate the ellipticity given a quadrupole shape.

        Parameters
        ----------
        shape : `lsst.afw.geom.ellipses.Quadrupole`
            The quadrupole shape

        Returns
        -------
        ellipticity : `float`
            The magnitude of the ellipticity
        """
        ixx = shape.getIxx()
        iyy = shape.getIyy()
        ixy = shape.getIxy()
        ePlus = (ixx - iyy) / (ixx + iyy)
        eCross = 2*ixy / (ixx + iyy)
        return (ePlus**2 + eCross**2)**0.5

    def getRoundestObject(self, footPrintSet, parentExp, fluxCut=1e-15):
        """Get the roundest object brighter than fluxCut from a footPrintSet.

        Parameters
        ----------
        footPrintSet : `lsst.afw.detection.FootprintSet`
            The set of footprints resulting from running detection on parentExp

        parentExp : `lsst.afw.image.exposure`
            The parent exposure for the footprint set.

        fluxCut : `float`
            The flux, below which, sources are rejected.

        Returns
        -------
        source : `lsst.afw.detection.Footprint`
            The winning footprint from the input footPrintSet
        """
        self.log.debug("ellipticity\tflux/1e6\tcentroid")
        sourceDict = {}
        for fp in footPrintSet.getFootprints():
            shape = fp.getShape()
            e = self._getEllipticity(shape)
            flux = fp.getSpans().flatten(parentExp.image.array, parentExp.image.getXY0()).sum()
            self.log.debug("%.4f\t%.2f\t%s"%(e, flux/1e6, str(fp.getCentroid())))
            if flux > fluxCut:
                sourceDict[e] = fp

        return sourceDict[sorted(sourceDict.keys())[0]]

    def getBrightestObject(self, footPrintSet, parentExp, roundnessCut=1e9):
        """Get the brightest object rounder than the cut from a footPrintSet.

        Parameters
        ----------
        footPrintSet : `lsst.afw.detection.FootprintSet`
            The set of footprints resulting from running detection on parentExp

        parentExp : `lsst.afw.image.exposure`
            The parent exposure for the footprint set.

        roundnessCut : `float`
            The ellipticity, above which, sources are rejected.

        Returns
        -------
        source : `lsst.afw.detection.Footprint`
            The winning footprint from the input footPrintSet
        """
        self.log.debug("ellipticity\tflux\tcentroid")
        sourceDict = {}
        for fp in footPrintSet.getFootprints():
            shape = fp.getShape()
            e = self._getEllipticity(shape)
            flux = fp.getSpans().flatten(parentExp.image.array, parentExp.image.getXY0()).sum()
            self.log.debug("%.4f\t%.2f\t%s"%(e, flux/1e6, str(fp.getCentroid())))
            if e < roundnessCut:
                sourceDict[flux] = fp

        return sourceDict[sorted(sourceDict.keys())[-1]]

    def findMainSource(self, exp):
        """Return the x,y of the brightest or roundest object in an exposure.

        Given a postISR exposure, run source detection on it, and return the
        centroid of the main star. Depending on the task configuration, this
        will either be the roundest object above a certain flux cutoff, or
        the brightest object which is rounder than some ellipticity cutoff.

        Parameters
        ----------
        exp : `afw.image.Exposure`
            The postISR exposure in which to find the main star

        Returns
        -------
        x, y : `tuple` of `float`
            The centroid of the main star in the image

        Notes
        -----
        Behavior of this method is controlled by many task config params
        including, for the detection stage:
        config.mainStarNpixMin
        config.mainStarNsigma
        config.mainStarGrow
        config.mainStarGrowIsotropic

        And post-detection, for selecting the main source:
        config.mainSourceFindingMethod
        config.mainStarFluxCut
        config.mainStarRoundnessCut
        """
        fpSet = self.findObjects(exp)
        if self.config.mainSourceFindingMethod == 'ROUNDEST':
            source = self.getRoundestObject(fpSet, exp, fluxCut=self.config.mainStarFluxCut)
        elif self.config.mainSourceFindingMethod == 'BRIGHTEST':
            source = self.getBrightestObject(fpSet, exp,
                                             roundnessCut=self.config.mainStarRoundnessCut)
        else:
            # should be impossible as this is a choice field, but still
            raise RuntimeError(f"Invalid source finding method"
                               "selected: {self.config.mainSourceFindingMethod}")
        return source.getCentroid()

    def updateMetadata(self, exp, **kwargs):
        md = exp.getMetadata()
        vi = exp.getInfo().getVisitInfo()

        ha = vi.getBoresightHourAngle().asDegrees()
        airmass = vi.getBoresightAirmass()

        md['HA'] = ha
        md.setComment('HA', 'Hour angle of observation start')

        md['AIRMASS'] = airmass
        md.setComment('AIRMASS', 'Airmass at observation start')

        if 'centroid' in kwargs:
            centroid = kwargs['centroid']
        else:
            centroid = (None, None)

        md['OBJECTX'] = centroid[0]
        md.setComment('OBJECTX', 'x pixel coordinate of object centroid')

        md['OBJECTY'] = centroid[1]
        md.setComment('OBJECTY', 'y pixel coordinate of object centroid')

        exp.setMetadata(md)

    def runDataRef(self, dataRef):
        """Run the ProcessStarTask on a ButlerDataRef for a single exposure.

        Runs isr to get the postISR exposure from the dataRef and passes this
        to the run() method.

        Parameters
        ----------
        dataRef : `daf.persistence.butlerSubset.ButlerDataRef`
            Butler reference of the detector and exposure ID
        """
        butler = dataRef.getButler()
        dataId = dataRef.dataId
        self.log.info("Processing %s" % (dataRef.dataId))

        if COMMISSIONING:
            from lsst.rapid.analysis.bestEffort import BestEffortIsr  # import here because not part of DM
            # TODO: some evidence suggests that CR repair is *significantly*
            # degrading spectractor performance investigate this for the
            # default task config as well as ensuring that it doesn't run here
            # if it does turn out to be problematic.
            bestEffort = BestEffortIsr(butler=dataRef.getButler())
            exposure = bestEffort.getExposure(dataId)
        else:
            if butler.datasetExists('postISRCCD', dataId):
                exposure = butler.get('postISRCCD', dataId)
                self.log.info("Loaded postISRCCD from disk")
            else:
                exposure = self.isr.runDataRef(dataRef).exposure
                self.updateMetadata(exposure)
                butler.put(exposure, 'postISRCCD', dataId)

        if butler.datasetExists('icExp', dataId) and butler.datasetExists('icSrc', dataId):
            exposure = butler.get('icExp', dataId)
            icSrc = butler.get('icSrc', dataId)
            self.log.info("Loaded icExp and icSrc from disk")
        else:
            charRes = self.charImage.runDataRef(dataRef=dataRef, exposure=exposure, doUnpersist=False)
            exposure = charRes.exposure
            icSrc = charRes.sourceCat
            butler.put(exposure, 'icExp', dataId)
            butler.put(icSrc, 'icSrc', dataId)

        if butler.datasetExists('calexp', dataId):
            exposure = butler.get('calexp', dataId)
            self.log.info("Loaded calexp from disk")
            md = exposure.getMetadata()
            sourceCentroid = (md['OBJECTX'], md['OBJECTY'])  # set in saved md if previous fit succeeded
        else:
            astromResult = self.runAstrometry(butler, exposure, icSrc)
            if astromResult and astromResult.scatterOnSky.asArcseconds() < 1:
                target = exposure.getMetadata()['OBJECT']
                sourceCentroid = getTargetCentroidFromWcs(exposure, target, logger=self.log)
            else:
                sourceCentroid = self.findMainSource(exposure)
                self.log.warn(f"Astrometric fit failed, failing over to source-finding centroid")
                self.log.info(f"Centroid of main star at: {sourceCentroid!r}")

            self.updateMetadata(exposure, centroid=sourceCentroid)
            butler.put(exposure, 'calexp', dataId)

            if self.debug.display and 'raw' in self.debug.displayItems:
                self.disp1.mtv(exposure)
                self.disp1.dot('x', sourceCentroid[0], sourceCentroid[1], size=100)
                self.log.info("Showing full postISR image")
                self.log.info(f"Centroid of main star at: {sourceCentroid}")
                self.pause()

        outputRoot = dataRef.getUri(datasetType='spectractorOutputRoot', write=True)
        if not os.path.exists(outputRoot):
            os.makedirs(outputRoot)
            if not os.path.exists(outputRoot):
                raise RuntimeError(f"Failed to create output dir {outputRoot}")
        expId = dataRef.dataId['expId']

        ret = self.run(exposure, outputRoot, expId, sourceCentroid)
        self.log.info("Finished processing %s" % (dataRef.dataId))

        return ret

    def runAstrometry(self, butler, exp, icSrc):
        refObjLoaderConfig = LoadIndexedReferenceObjectsTask.ConfigClass()
        refObjLoaderConfig.ref_dataset_name = 'gaia_dr2_20191105'
        refObjLoaderConfig.pixelMargin = 1000
        refObjLoader = LoadIndexedReferenceObjectsTask(butler=butler, config=refObjLoaderConfig)

        astromConfig = AstrometryTask.ConfigClass()
        astromConfig.wcsFitter.retarget(FitAffineWcsTask)
        astromConfig.referenceSelector.doMagLimit = True
        magLimit = MagnitudeLimit()
        magLimit.minimum = 1
        magLimit.maximum = 15
        astromConfig.referenceSelector.magLimit = magLimit
        astromConfig.referenceSelector.magLimit.fluxField = "phot_g_mean_flux"
        astromConfig.matcher.maxRotationDeg = 5.99
        astromConfig.matcher.maxOffsetPix = 3000
        astromConfig.sourceSelector['matcher'].minSnr = 10
        solver = AstrometryTask(config=astromConfig, refObjLoader=refObjLoader)

        # TODO: Change this to doing this the proper way
        referenceFilterName = self.config.referenceFilterOverride
        defineFilter(referenceFilterName, 656.28)
        referenceFilter = afwImage.filter.Filter(referenceFilterName)
        originalFilter = exp.getFilter()  # there's a better way of doing this with the task itself I think
        exp.setFilter(referenceFilter)

        try:
            astromResult = solver.run(sourceCat=icSrc, exposure=exp)
            exp.setFilter(originalFilter)
        except Exception:
            self.log.warn("Solver failed to run completely")
            return None

        scatter = astromResult.scatterOnSky.asArcseconds()
        if scatter < 1:
            return astromResult
        else:
            self.log.warn("Failed to find an acceptable match")
        return None

    def pause(self):
        if self.debug.pauseOnDisplay:
            input("Press return to continue...")
        return

    def loadStarNames(self):
        starNameFile = os.path.join(getPackageDir('atmospec'), 'data', 'starNames.txt')
        with open(starNameFile, 'r') as f:
            lines = f.readlines()
        return [line.strip() for line in lines]

    def run(self, exp, spectractorOutputRoot, expId, sourceCentroid):
        """Calculate the wavelength calibrated 1D spectrum from a postISRCCD.

        An outline of the steps in the processing is as follows:
         * Source extraction - find the objects in image
         * Process sources to find the x,y of the main star

         * Given the centroid, the dispersion direction, and the order(s),
           calculate the spectrum's bounding box

         * (Rotate the image such that the dispersion direction is vertical
            TODO: DM-18138)

         * Create an initial dispersion relation object from the geometry
           or alternative bootstrapping method

         * Apply an initial flatfielding - TODO: DM-18141

         * Find and interpolate over cosmics if necessary - TODO: DM-18140

         * Perform an initial spectral extraction, depending on selected method
         *     Fit a background model and subtract
         *     Perform row-wise fits for extraction
         *     TODO: DM-18136 for doing a full-spectrum fit with PSF model

         * Given knowledge of features in the spectrum, find lines in the
           measured spectrum and re-fit to refine the dispersion relation
         * Reflatfield the image with the refined dispersion relation

        Parameters
        ----------
        exp : `afw.image.Exposure`
            The postISR exposure in which to find the main star

        Returns
        -------
        spectrum : `lsst.atmospec.spectrum` - TODO: DM-18133
            The wavelength-calibrated 1D stellar spectrum
        """
        reload(plt)  # reset matplotlib color cycles when multiprocessing
        pdfPath = os.path.join(spectractorOutputRoot, 'plots.pdf')
        starNames = self.loadStarNames()
        with PdfPages(pdfPath) as pdf:
            if True:
                overrideDict = {'SAVE': True,
                                'OBS_NAME': 'AUXTEL',
                                'DEBUG': True,
                                'DISPLAY': False,
                                'VERBOSE': True,
                                # 'CCD_IMSIZE': 4000}
                                }
                supplementDict = {'CALLING_CODE': 'LSST_DM',
                                  'STAR_NAMES': starNames}
                resetParameters = {'PdfPages': pdf,  # anything that changes between dataRefs!
                                   'LSST_SAVEFIGPATH': spectractorOutputRoot}
            else:
                overrideDict = {}
                supplementDict = {}

            overrideDict['DISTANCE2CCD'] = 175  # TODO: change to a calculation based on LINPOS

            # Note - flow is that the config file is loaded, then overrides are
            # applied, then supplements are set.
            configFilename = '/home/mfl/lsst/Spectractor/config/auxtel.ini'
            spectractor = SpectractorShim(configFile=configFilename,
                                          paramOverrides=overrideDict,
                                          supplementaryParameters=supplementDict,
                                          resetParameters=resetParameters)

            target = exp.getMetadata()['OBJECT']
            if self.config.forceObjectName:
                self.log.warn(f"Forcing target name from {target} to {self.config.forceObjectName}")
                target = self.config.forceObjectName

            if target in ['FlatField position', 'Park position', 'Test', 'NOTSET']:
                raise ValueError(f"OBJECT set to {target} - this is not a celestial object!")

            spectractor.run(exp, *sourceCentroid, target, spectractorOutputRoot, expId)

        return

    def flatfield(self, exp, disp):
        """Placeholder for wavelength dependent flatfielding: TODO: DM-18141

        Will probably need a dataRef, as it will need to be retrieving flats
        over a range. Also, it will be somewhat complex, so probably needs
        moving to its own task"""
        self.log.warn("Flatfielding not yet implemented")
        return exp

    def repairCosmics(self, exp, disp):
        self.log.warn("Cosmic ray repair not yet implemented")
        return exp

    def measureSpectrum(self, exp, sourceCentroid, spectrumBBox, dispersionRelation):
        """Perform the spectral extraction, given a source location and exp."""

        self.extraction.initialise(exp, sourceCentroid, spectrumBBox, dispersionRelation)

        # xxx this method currently doesn't return an object - fix this
        spectrum = self.extraction.getFluxBasic()

        return spectrum

    def calcSpectrumBBox(self, exp, centroid, aperture, order='+1'):
        """Calculate the bbox for the spectrum, given the centroid.

        XXX Longer explanation here, inc. parameters
        TODO: Add support for order = "both"
        """
        extent = self.config.spectrumLengthPixels
        halfWidth = aperture//2
        translate_y = self.config.offsetFromMainStar
        sourceX = centroid[0]
        sourceY = centroid[1]

        if(order == '-1'):
            translate_y = - extent - self.config.offsetFromMainStar

        xStart = sourceX - halfWidth
        xEnd = sourceX + halfWidth - 1
        yStart = sourceY + translate_y
        yEnd = yStart + extent - 1

        xEnd = min(xEnd, exp.getWidth()-1)
        yEnd = min(yEnd, exp.getHeight()-1)
        yStart = max(yStart, 0)
        xStart = max(xStart, 0)
        assert (xEnd > xStart) and (yEnd > yStart)

        self.log.debug('(xStart, xEnd) = (%s, %s)'%(xStart, xEnd))
        self.log.debug('(yStart, yEnd) = (%s, %s)'%(yStart, yEnd))

        bbox = afwGeom.Box2I(afwGeom.Point2I(xStart, yStart), afwGeom.Point2I(xEnd, yEnd))
        return bbox

        # def calcRidgeLine(self, footprint):
        #     ridgeLine = np.zeros(self.footprint.length)
        #     for

        #     return ridgeLine
