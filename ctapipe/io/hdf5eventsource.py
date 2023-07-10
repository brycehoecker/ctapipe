import logging
from functools import lru_cache
from pathlib import Path
from typing import Dict

import astropy.units as u
import numpy as np
import tables
from astropy.utils.decorators import lazyproperty

from ctapipe.atmosphere import AtmosphereDensityProfile
from ctapipe.instrument.optics import FocalLengthKind

from ..containers import (
    ArrayEventIndexContainer,
    CameraHillasParametersContainer,
    CameraTimingParametersContainer,
    ConcentrationContainer,
    DL0SubarrayContainer,
    DL0TelescopeContainer,
    DL1TelescopeContainer,
    DL2TelescopeContainer,
    HillasParametersContainer,
    ImageParametersContainer,
    IntensityStatisticsContainer,
    LeakageContainer,
    MorphologyContainer,
    MuonContainer,
    MuonEfficiencyContainer,
    MuonParametersContainer,
    MuonRingContainer,
    ObservationBlockContainer,
    ParticleClassificationContainer,
    PeakTimeStatisticsContainer,
    R1TelescopeContainer,
    ReconstructedEnergyContainer,
    ReconstructedGeometryContainer,
    SchedulingBlockContainer,
    SimulatedShowerContainer,
    SimulationConfigContainer,
    SimulationSubarrayContainer,
    SimulationTelescopeContainer,
    SubarrayEventContainer,
    SubarrayTriggerContainer,
    TelescopeEventContainer,
    TelescopeEventIndexContainer,
    TelescopeImpactParameterContainer,
    TelescopePointingContainer,
    TelescopeTriggerContainer,
    TimingParametersContainer,
)
from ..core import Container, Field
from ..core.traits import UseEnum
from ..instrument import SubarrayDescription
from ..utils import IndexFinder
from .astropy_helpers import read_table
from .datalevels import DataLevel
from .eventsource import EventSource
from .hdf5tableio import HDF5TableReader, get_column_attrs
from .tableloader import DL2_SUBARRAY_GROUP, DL2_TELESCOPE_GROUP

__all__ = ["HDF5EventSource"]


logger = logging.getLogger(__name__)


DL2_CONTAINERS = {
    "energy": ReconstructedEnergyContainer,
    "geometry": ReconstructedGeometryContainer,
    "classification": ParticleClassificationContainer,
    "impact": TelescopeImpactParameterContainer,
}


COMPATIBLE_DATA_MODEL_VERSIONS = [
    "v4.0.0",
    "v5.0.0",
]


def get_hdf5_datalevels(h5file):
    """Get the data levels present in the hdf5 file"""
    datalevels = []

    if "/r1/event/telescope" in h5file.root:
        datalevels.append(DataLevel.R1)

    if "/dl1/event/telescope/images" in h5file.root:
        datalevels.append(DataLevel.DL1_IMAGES)

    if "/dl1/event/telescope/parameters" in h5file.root:
        datalevels.append(DataLevel.DL1_PARAMETERS)

    if "/dl1/event/telescope/muon" in h5file.root:
        datalevels.append(DataLevel.DL1_MUON)

    if "/dl2" in h5file.root:
        datalevels.append(DataLevel.DL2)

    return tuple(datalevels)


def read_atmosphere_density_profile(
    h5file: tables.File, path="/simulation/service/atmosphere_density_profile"
):
    """return a subclass of AtmosphereDensityProfile by
    reading a table in a h5 file

    Parameters
    ----------
    h5file: tables.File
        file handle of HDF5 file
    path: str
        path in the file where the serialized model is stored as an
        astropy.table.Table

    Returns
    -------
    AtmosphereDensityProfile:
        subclass depending on type of table
    """

    if path not in h5file:
        return None

    table = read_table(h5file=h5file, path=path)
    return AtmosphereDensityProfile.from_table(table)


class HDF5EventSource(EventSource):
    """
    Event source for files in the ctapipe DL1 format.
    For general information about the concept of event sources,
    take a look at the parent class ctapipe.io.EventSource.

    To use this event source, create an instance of this class
    specifying the file to be read.

    Looping over the EventSource yields events from the _generate_events
    method. An event equals an ArrayEventContainer instance.
    See ctapipe.containers.ArrayEventContainer for details.

    Attributes
    ----------
    input_url: str
        Path to the input event file.
    file: tables.File
        File object
    obs_ids: list
        Observation ids of te recorded runs. For unmerged files, this
        should only contain a single number.
    subarray: ctapipe.instrument.SubarrayDescription
        The subarray configuration of the recorded run.
    datalevels: Tuple
        One or both of ctapipe.io.datalevels.DataLevel.DL1_IMAGES
        and ctapipe.io.datalevels.DataLevel.DL1_PARAMETERS
        depending on the information present in the file.
    is_simulation: Boolean
        Whether the events are simulated or observed.
    simulation_configs: Dict
        Mapping of obs_id to ctapipe.containers.SimulationConfigContainer
        if the file contains simulated events.
    has_simulated_dl1: Boolean
        Whether the file contains simulated camera images and/or
        image parameters evaluated on these.
    """

    focal_length_choice = UseEnum(
        FocalLengthKind,
        default_value=FocalLengthKind.EFFECTIVE,
        help=(
            "If both nominal and effective focal lengths are available, "
            " which one to use for the `~ctapipe.coordinates.CameraFrame` attached"
            " to the `~ctapipe.instrument.CameraGeometry` instances in the"
            " `~ctapipe.instrument.SubarrayDescription` which will be used in"
            " CameraFrame to TelescopeFrame coordinate transforms."
            " The 'nominal' focal length is the one used during "
            " the simulation, the 'effective' focal length is computed using specialized "
            " ray-tracing from a point light source"
        ),
    ).tag(config=True)

    def __init__(self, input_url=None, config=None, parent=None, **kwargs):
        """
        EventSource for dl1 files in the standard DL1 data format

        Parameters:
        -----------
        input_url : str
            Path of the file to load
        config : traitlets.loader.Config
            Configuration specified by config file or cmdline arguments.
            Used to set traitlet values.
            Set to None if no configuration to pass.
        parent:
            Parent from which the config is used. Mutually exclusive with config
        kwargs
        """
        super().__init__(input_url=input_url, config=config, parent=parent, **kwargs)

        self.file_ = tables.open_file(self.input_url)
        self._full_subarray = SubarrayDescription.from_hdf(
            self.input_url,
            focal_length_choice=self.focal_length_choice,
        )

        if self.allowed_tels:
            self._subarray = self._full_subarray.select_subarray(self.allowed_tels)
            self._allowed_tels_array = np.array(list(self.allowed_tels))
        else:
            self._subarray = self._full_subarray

        self._simulation_configs = self._parse_simulation_configs()
        self._scheduling_block, self._observation_block = self._parse_sb_and_ob()

        version = self.file_.root._v_attrs["CTA PRODUCT DATA MODEL VERSION"]
        self.datamodel_version = tuple(map(int, version.lstrip("v").split(".")))
        self._obs_ids = tuple(
            self.file_.root.configuration.observation.observation_block.col("obs_id")
        )

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def close(self):
        self.file_.close()

    @staticmethod
    def is_compatible(file_path):
        path = Path(file_path).expanduser()
        if not path.is_file():
            return False

        with path.open("rb") as f:
            magic_number = f.read(8)

        if magic_number != b"\x89HDF\r\n\x1a\n":
            return False

        with tables.open_file(path) as f:
            metadata = f.root._v_attrs

            if "CTA PRODUCT DATA MODEL VERSION" not in metadata._v_attrnames:
                return False

            version = metadata["CTA PRODUCT DATA MODEL VERSION"]
            if version not in COMPATIBLE_DATA_MODEL_VERSIONS:
                logger.error(
                    "File is a ctapipe HDF5 file but has unsupported data model"
                    f" version {version}"
                    f", supported versions are {COMPATIBLE_DATA_MODEL_VERSIONS}."
                    " You may need to downgrade ctapipe (if the file version is older)"
                    ", update ctapipe (if the file version is newer) or"
                    " reproduce the file with your current ctapipe version."
                )
                return False

            if "CTA PRODUCT DATA LEVELS" not in metadata._v_attrnames:
                return False

            # we can now read both R1 and DL1
            has_muons = "/dl1/event/telescope/muon" in f.root
            datalevels = set(metadata["CTA PRODUCT DATA LEVELS"].split(","))
            datalevels = datalevels & {
                "R1",
                "DL1_IMAGES",
                "DL1_PARAMETERS",
                "DL2",
                "DL1_MUON",
            }
            if not datalevels and not has_muons:
                return False

        return True

    @property
    def is_simulation(self):
        """
        True for files with a simulation group at the root of the file.
        """
        return "simulation" in self.file_.root

    @property
    def has_simulated_dl1(self):
        """
        True for files with telescope-wise event information in the simulation group
        """
        if self.is_simulation:
            if "telescope" in self.file_.root.simulation.event:
                return True
        return False

    @lazyproperty
    def has_muon_parameters(self):
        """
        True for files that contain muon parameters
        """
        return "/dl1/event/telescope/muon" in self.file_.root

    @property
    def subarray(self):
        return self._subarray

    @lazyproperty
    def datalevels(self):
        return get_hdf5_datalevels(self.file_)

    @lazyproperty
    def atmosphere_density_profile(self) -> AtmosphereDensityProfile:
        return read_atmosphere_density_profile(self.file_)

    @property
    def obs_ids(self):
        return self._obs_ids

    @property
    def scheduling_blocks(self) -> Dict[int, SchedulingBlockContainer]:
        return self._scheduling_block

    @property
    def observation_blocks(self) -> Dict[int, ObservationBlockContainer]:
        return self._observation_block

    @property
    def simulation_config(self) -> Dict[int, SimulationConfigContainer]:
        """
        Returns the simulation config(s) as
        a dict mapping obs_id to the respective config.
        """
        return self._simulation_configs

    def __len__(self):
        n_events = len(self.file_.root.dl0.event.subarray.trigger)
        if self.max_events is not None:
            return min(n_events, self.max_events)
        return n_events

    def _parse_simulation_configs(self):
        """
        Construct a dict of SimulationConfigContainers from the
        self.file_.root.configuration.simulation.run.
        These are used to match the correct header to each event
        """
        # Just returning next(reader) would work as long as there are no merged files
        # The reader ignores obs_id making the setup somewhat tricky
        # This is ugly but supports multiple headers so each event can have
        # the correct mcheader assigned by matching the obs_id
        # Alternatively this becomes a flat list
        # and the obs_id matching part needs to be done in _generate_events()
        class ObsIdContainer(Container):
            default_prefix = ""
            obs_id = Field(-1)

        if "simulation" in self.file_.root.configuration:
            reader = HDF5TableReader(self.file_).read(
                "/configuration/simulation/run",
                containers=(SimulationConfigContainer, ObsIdContainer),
            )
            return {index.obs_id: config for (config, index) in reader}
        else:
            return {}

    def _parse_sb_and_ob(self):
        """read Observation and Scheduling block configurations"""

        sb_reader = HDF5TableReader(self.file_).read(
            "/configuration/observation/scheduling_block",
            containers=SchedulingBlockContainer,
        )

        scheduling_blocks = {sb.sb_id: sb for sb in sb_reader}

        ob_reader = HDF5TableReader(self.file_).read(
            "/configuration/observation/observation_block",
            containers=ObservationBlockContainer,
        )
        observation_blocks = {ob.obs_id: ob for ob in ob_reader}

        return scheduling_blocks, observation_blocks

    def _is_hillas_in_camera_frame(self):
        parameters_group = self.file_.root.dl1.event.telescope.parameters
        telescope_tables = parameters_group._v_children.values()

        # in case of no parameters, it doesn't matter, we just return False
        if len(telescope_tables) == 0:
            return False

        # check the first telescope table
        one_telescope = parameters_group._v_children.values()[0]
        return "camera_frame_hillas_intensity" in one_telescope.colnames

    def _generator(self):
        """
        Yield ArrayEventContainer to iterate through events.
        """
        self.reader = HDF5TableReader(self.file_)

        if DataLevel.R1 in self.datalevels:
            waveform_readers = {
                table.name: self.reader.read(
                    f"/r1/event/telescope/{table.name}", R1TelescopeContainer
                )
                for table in self.file_.root.r1.event.telescope
            }

        if DataLevel.DL1_IMAGES in self.datalevels:
            ignore_columns = {"parameters"}

            # if there are no parameters, there are no image_mask, avoids warnings
            if DataLevel.DL1_PARAMETERS not in self.datalevels:
                ignore_columns.add("image_mask")

            image_readers = {
                table.name: self.reader.read(
                    f"/dl1/event/telescope/images/{table.name}",
                    DL1TelescopeContainer,
                    ignore_columns=ignore_columns,
                )
                for table in self.file_.root.dl1.event.telescope.images
            }
            if self.has_simulated_dl1:
                simulated_image_iterators = {
                    table.name: self.file_.root.simulation.event.telescope.images[
                        table.name
                    ].iterrows()
                    for table in self.file_.root.simulation.event.telescope.images
                }

        if DataLevel.DL1_PARAMETERS in self.datalevels:
            hillas_cls = HillasParametersContainer
            timing_cls = TimingParametersContainer
            hillas_prefix = "hillas"
            timing_prefix = "timing"

            if self._is_hillas_in_camera_frame():
                hillas_cls = CameraHillasParametersContainer
                timing_cls = CameraTimingParametersContainer
                hillas_prefix = "camera_frame_hillas"
                timing_prefix = "camera_frame_timing"

            param_readers = {
                table.name: self.reader.read(
                    f"/dl1/event/telescope/parameters/{table.name}",
                    containers=(
                        hillas_cls,
                        timing_cls,
                        LeakageContainer,
                        ConcentrationContainer,
                        MorphologyContainer,
                        IntensityStatisticsContainer,
                        PeakTimeStatisticsContainer,
                    ),
                    prefixes=[
                        hillas_prefix,
                        timing_prefix,
                        "leakage",
                        "concentration",
                        "morphology",
                        "intensity",
                        "peak_time",
                    ],
                )
                for table in self.file_.root.dl1.event.telescope.parameters
            }
            if self.has_simulated_dl1:
                simulated_param_readers = {
                    table.name: self.reader.read(
                        f"/simulation/event/telescope/parameters/{table.name}",
                        containers=[
                            hillas_cls,
                            LeakageContainer,
                            ConcentrationContainer,
                            MorphologyContainer,
                            IntensityStatisticsContainer,
                        ],
                        prefixes=[
                            f"true_{hillas_prefix}",
                            "true_leakage",
                            "true_concentration",
                            "true_morphology",
                            "true_intensity",
                        ],
                    )
                    for table in self.file_.root.dl1.event.telescope.parameters
                }

        if self.has_muon_parameters:
            muon_readers = {
                table.name: self.reader.read(
                    f"/dl1/event/telescope/muon/{table.name}",
                    containers=[
                        MuonRingContainer,
                        MuonParametersContainer,
                        MuonEfficiencyContainer,
                    ],
                )
                for table in self.file_.root.dl1.event.telescope.muon
            }

        dl2_readers = {}
        if DL2_SUBARRAY_GROUP in self.file_.root:
            dl2_group = self.file_.root[DL2_SUBARRAY_GROUP]

            for kind, group in dl2_group._v_children.items():

                try:
                    container = DL2_CONTAINERS[kind]
                except KeyError:
                    self.log.warning("Unknown DL2 stereo group %s", kind)
                    continue

                dl2_readers[kind] = {
                    algorithm: HDF5TableReader(self.file_).read(
                        table._v_pathname,
                        containers=container,
                        prefixes=(algorithm,),
                    )
                    for algorithm, table in group._v_children.items()
                }

        dl2_tel_readers = {}
        if DL2_TELESCOPE_GROUP in self.file_.root:
            dl2_group = self.file_.root[DL2_TELESCOPE_GROUP]

            for kind, group in dl2_group._v_children.items():
                try:
                    container = DL2_CONTAINERS[kind]
                except KeyError:
                    self.log.warning("Unknown DL2 telescope group %s", kind)
                    continue

                dl2_tel_readers[kind] = {}
                for algorithm, algorithm_group in group._v_children.items():
                    dl2_tel_readers[kind][algorithm] = {
                        key: HDF5TableReader(self.file_).read(
                            table._v_pathname,
                            containers=container,
                        )
                        for key, table in algorithm_group._v_children.items()
                    }

        true_impact_readers = {}
        if self.is_simulation:
            # simulated shower wide information
            mc_shower_reader = HDF5TableReader(self.file_).read(
                "/simulation/event/subarray/shower",
                SimulatedShowerContainer,
                prefixes="true",
            )
            if "impact" in self.file_.root.simulation.event.telescope:
                true_impact_readers = {
                    table.name: self.reader.read(
                        f"/simulation/event/telescope/impact/{table.name}",
                        containers=TelescopeImpactParameterContainer,
                        prefixes=["true_impact"],
                    )
                    for table in self.file_.root.simulation.event.telescope.impact
                }

        # Setup iterators for the array events
        events = HDF5TableReader(self.file_).read(
            "/dl0/event/subarray/trigger",
            [SubarrayTriggerContainer, ArrayEventIndexContainer],
            ignore_columns={"tel"},
        )
        telescope_trigger_reader = HDF5TableReader(self.file_).read(
            "/dl0/event/telescope/trigger",
            [TelescopeEventIndexContainer, TelescopeTriggerContainer],
            ignore_columns={"trigger_pixels"},
        )

        table_path = "/dl1/monitoring/subarray/pointing"
        if table_path not in self.file_.root:
            table_path = "/dl0/monitoring/subarray/pointing"

        array_pointing_finder = IndexFinder(self.file_.root[table_path].col("time"))

        group_path = "/dl1/monitoring/telescope/pointing"
        if group_path not in self.file_.root:
            group_path = "/dl0/monitoring/telescope/pointing"
        tel_pointing_finder = {
            table.name: IndexFinder(table.col("time"))
            for table in self.file_.root[group_path]
        }

        counter = 0
        for trigger, index in events:
            event = SubarrayEventContainer(
                dl0=DL0SubarrayContainer(trigger=trigger),
                count=counter,
                index=index,
                simulation=SimulationSubarrayContainer()
                if self.is_simulation
                else None,
            )
            # Maybe take some other metadata, but there are still some 'unknown'
            # written out by the stage1 tool
            event.meta["origin"] = self.file_.root._v_attrs["CTA PROCESS TYPE"]
            event.meta["input_url"] = self.input_url
            event.meta["max_events"] = self.max_events

            event.dl0.trigger.tels_with_trigger = (
                self._full_subarray.tel_mask_to_tel_ids(
                    event.dl0.trigger.tels_with_trigger
                )
            )
            full_tels_with_trigger = event.dl0.trigger.tels_with_trigger.copy()
            if self.allowed_tels:
                event.dl0.trigger.tels_with_trigger = np.intersect1d(
                    event.dl0.trigger.tels_with_trigger,
                    self._allowed_tels_array,
                )

            if self.is_simulation:
                event.simulation.shower = next(mc_shower_reader)

            for kind, readers in dl2_readers.items():
                c = getattr(event.dl2, kind)
                for algorithm, reader in readers.items():
                    c[algorithm] = next(reader)

            self._fill_array_pointing(event, array_pointing_finder)

            # the telescope trigger table contains triggers for all telescopes
            # that participated in the event, so we need to read a row for each
            # of them, ignoring the ones not in allowed_tels after reading
            for tel_id in full_tels_with_trigger:
                tel_index, tel_trigger = next(telescope_trigger_reader)

                if self.allowed_tels and tel_id not in self.allowed_tels:
                    continue

                key = f"tel_{tel_id:03d}"

                containers = {
                    "index": tel_index,
                    "dl0": DL0TelescopeContainer(trigger=tel_trigger),
                }

                containers["pointing"] = self._fill_telescope_pointing(
                    tel_id, tel_trigger.time, tel_pointing_finder
                )

                if DataLevel.R1 in self.datalevels:
                    containers["r1"] = next(waveform_readers[key])

                if self.is_simulation:
                    containers["simulation"] = SimulationTelescopeContainer()

                if key in true_impact_readers:
                    containers["simulation"].impact = next(true_impact_readers[key])

                if DataLevel.DL1_IMAGES in self.datalevels:
                    containers["dl1"] = next(image_readers[key])

                    if self.has_simulated_dl1:
                        simulated_image_row = next(simulated_image_iterators[key])
                        containers["simulation"].true_image = simulated_image_row[
                            "true_image"
                        ]
                else:
                    containers["dl1"] = DL1TelescopeContainer()

                if DataLevel.DL1_PARAMETERS in self.datalevels:
                    # Is there a smarter way to unpack this?
                    # Best would probbaly be if we could directly read
                    # into the ImageParametersContainer
                    params = next(param_readers[key])
                    containers["dl1"].parameters = ImageParametersContainer(
                        hillas=params[0],
                        timing=params[1],
                        leakage=params[2],
                        concentration=params[3],
                        morphology=params[4],
                        intensity_statistics=params[5],
                        peak_time_statistics=params[6],
                    )
                    if self.has_simulated_dl1:
                        simulated_params = next(simulated_param_readers[key])
                        containers[
                            "simulation"
                        ].true_parameters = ImageParametersContainer(
                            hillas=simulated_params[0],
                            leakage=simulated_params[1],
                            concentration=simulated_params[2],
                            morphology=simulated_params[3],
                            intensity_statistics=simulated_params[4],
                        )

                if self.has_muon_parameters:
                    ring, parameters, efficiency = next(muon_readers[key])
                    containers["muon"] = MuonContainer(
                        ring=ring,
                        parameters=parameters,
                        efficiency=efficiency,
                    )

                dl2 = DL2TelescopeContainer()
                for kind, algorithms in dl2_tel_readers.items():
                    c = getattr(dl2, kind)
                    for algorithm, readers in algorithms.items():
                        c[algorithm] = next(readers[key])

                        # change prefix to new data model
                        if kind == "impact" and self.datamodel_version == (4, 0, 0):
                            prefix = f"{algorithm}_tel_{c[algorithm].default_prefix}"
                            c[algorithm].prefix = prefix

                event.tel[tel_id] = TelescopeEventContainer(**containers, dl2=dl2)

            # this needs to stay *after* reading the telescope trigger table
            # and after reading all subarray event information, so that we don't
            # go out of sync
            if len(event.tel) == 0:
                continue

            yield event
            counter += 1

    @lazyproperty
    def _subarray_pointing_attrs(self):
        table = self.file_.root.dl0.monitoring.subarray.pointing
        return get_column_attrs(table)

    @lru_cache(maxsize=1000)
    def _telescope_pointing_attrs(self, tel_id):
        pointing_group = self.file_.root.dl0.monitoring.telescope.pointing
        return get_column_attrs(pointing_group[f"tel_{tel_id:03d}"])

    def _fill_array_pointing(self, event, array_pointing_finder):
        """
        Fill the array pointing information of a given event
        """
        # Only unique pointings are stored, so reader.read() wont work as easily
        # Thats why we match the pointings based on trigger time
        closest_time_index = array_pointing_finder.closest(event.dl0.trigger.time.mjd)
        table = self.file_.root.dl0.monitoring.subarray.pointing
        array_pointing = table[closest_time_index]

        event.pointing.azimuth = u.Quantity(
            array_pointing["azimuth"],
            self._subarray_pointing_attrs["azimuth"]["UNIT"],
        )
        event.pointing.altitude = u.Quantity(
            array_pointing["altitude"],
            self._subarray_pointing_attrs["altitude"]["UNIT"],
        )
        event.pointing.ra = u.Quantity(
            array_pointing["ra"],
            self._subarray_pointing_attrs["ra"]["UNIT"],
        )
        event.pointing.dec = u.Quantity(
            array_pointing["dec"],
            self._subarray_pointing_attrs["dec"]["UNIT"],
        )

    def _fill_telescope_pointing(self, tel_id, time, tel_pointing_finder):
        """
        Fill the telescope pointing information of a given event
        """
        # Same comments as to _fill_array_pointing apply
        pointing_group = self.file_.root.dl0.monitoring.telescope.pointing
        key = f"tel_{tel_id:03d}"

        tel_pointing_table = pointing_group[key]
        closest_time_index = tel_pointing_finder[key].closest(time.mjd)

        pointing_telescope = tel_pointing_table[closest_time_index]
        attrs = self._telescope_pointing_attrs(tel_id)
        return TelescopePointingContainer(
            azimuth=u.Quantity(
                pointing_telescope["azimuth"],
                attrs["azimuth"]["UNIT"],
            ),
            altitude=u.Quantity(
                pointing_telescope["altitude"],
                attrs["altitude"]["UNIT"],
            ),
        )
