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
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the LSST License Statement and
# the GNU General Public License along with this program.  If not,
# see <https://www.lsstcorp.org/LegalNotices/>.
#

import copy


def makeGainFlat(detectorExposure, gainDict):
    detector = detectorExposure.getDetector()
    ampNames = set(list(a.getName() for a in detector))
    assert set(gainDict.keys()) == ampNames

    flat = copy.copy(detectorExposure)
    for amp in detector:
        bbox = amp.getBBox()
        flat[bbox].maskedImage.image.array[:, :] = gainDict[amp.getName()]
    flat.maskedImage.mask[:] = 0x0
    flat.maskedImage.variance[:] = 0.0

    return flat
