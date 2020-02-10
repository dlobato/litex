# This file is Copyright (c) 2014-2020 Florent Kermarrec <florent@enjoy-digital.fr>
# This file is Copyright (c) 2013-2014 Sebastien Bourdeauducq <sb@m-labs.hk>
# This file is Copyright (c) 2019 Gabriel L. Somlo <somlo@cmu.edu>
# License: BSD

import logging
import time
import datetime
from math import log2

from migen import *

from litex.soc.cores import cpu
from litex.soc.cores.identifier import Identifier
from litex.soc.cores.timer import Timer

from litex.soc.interconnect.csr import *
from litex.soc.interconnect import csr_bus
from litex.soc.interconnect import wishbone
from litex.soc.interconnect import wishbone2csr
from litex.soc.interconnect import axi

from litedram.core import LiteDRAMCore
from litedram.frontend.wishbone import LiteDRAMWishbone2Native

# TODO:
# - replace raise with exit on logging error.
# - add configurable CSR paging.
# - manage SoCLinkerRegion
# - cleanup SoCCSRRegion

logging.basicConfig(level=logging.INFO)

# Helpers ------------------------------------------------------------------------------------------
def colorer(s, color="bright"):
    header  = {
        "bright": "\x1b[1m",
        "green":  "\x1b[32m",
        "cyan":   "\x1b[36m",
        "red":    "\x1b[31m",
        "yellow": "\x1b[33m",
        "underline": "\x1b[4m"}[color]
    trailer = "\x1b[0m"
    return header + str(s) + trailer

def build_time(with_time=True):
    fmt = "%Y-%m-%d %H:%M:%S" if with_time else "%Y-%m-%d"
    return datetime.datetime.fromtimestamp(time.time()).strftime("%Y-%m-%d %H:%M:%S")

# SoCConstant --------------------------------------------------------------------------------------

def SoCConstant(value):
    return value

# SoCRegion ----------------------------------------------------------------------------------------

class SoCRegion:
    def __init__(self, origin=None, size=None, mode="rw", cached=True):
        self.logger    = logging.getLogger("SoCRegion")
        self.origin    = origin
        self.size      = size
        self.mode      = mode
        self.cached    = cached

    def decoder(self):
        origin = self.origin
        size   = self.size
        origin &= ~0x80000000
        size   = 2**log2_int(size, False)
        if (origin & (size - 1)) != 0:
            self.logger.error("Origin needs to be aligned on size:")
            self.logger.error(self)
            raise
        origin >>= 2 # bytes to words aligned
        size   >>= 2 # bytes to words aligned
        return lambda a: (a[log2_int(size):-1] == (origin >> log2_int(size)))

    def __str__(self):
        r = ""
        if self.origin is not None:
            r += "Origin: {}, ".format(colorer("0x{:08x}".format(self.origin)))
        if self.size is not None:
            r += "Size: {}, ".format(colorer("0x{:08x}".format(self.size)))
        r += "Mode: {}, ".format(colorer(self.mode.upper()))
        r += "Cached: {}".format(colorer(self.cached))
        return r

class SoCIORegion(SoCRegion): pass

class SoCLinkerRegion(SoCRegion): pass

# SoCCSRRegion -------------------------------------------------------------------------------------

class SoCCSRRegion:
    def __init__(self, origin, busword, obj):
        self.origin  = origin
        self.busword = busword
        self.obj     = obj

# SoCBusHandler ------------------------------------------------------------------------------------

class SoCBusHandler(Module):
    supported_standard      = ["wishbone"]
    supported_data_width    = [32, 64]
    supported_address_width = [32]

    # Creation -------------------------------------------------------------------------------------
    def __init__(self, standard, data_width=32, address_width=32, timeout=1e6, reserved_regions={}):
        self.logger = logging.getLogger("SoCBusHandler")
        self.logger.info(colorer("Creating new Bus Handler..."))

        # Check Standard
        if standard not in self.supported_standard:
            self.logger.error("Unsupported Standard: {} supporteds: {:s}".format(
                colorer(standard, color="red"),
                colorer(", ".join(self.supported_standard), color="green")))
            raise

        # Check Data Width
        if data_width not in self.supported_data_width:
            self.logger.error("Unsupported Data_Width: {} supporteds: {:s}".format(
                colorer(data_width, color="red"),
                colorer(", ".join(str(x) for x in self.supported_data_width), color="green")))
            raise

        # Check Address Width
        if address_width not in self.supported_address_width:
            self.logger.error("Unsupported Address Width: {} supporteds: {:s}".format(
                colorer(data_width, color="red"),
                colorer(", ".join(str(x) for x in self.supported_address_width), color="green")))
            raise

        # Create Bus
        self.standard      = standard
        self.data_width    = data_width
        self.address_width = address_width
        self.masters       = {}
        self.slaves        = {}
        self.regions       = {}
        self.io_regions    = {}
        self.ld_regions    = {}
        self.timeout       = timeout
        self.logger.info("{}-bit {} Bus, {}GiB Address Space.".format(
            colorer(data_width), colorer(standard), colorer(2**address_width/2**30)))

        # Adding reserved regions
        self.logger.info("Adding {} Regions...".format(colorer("reserved")))
        for name, region in reserved_regions.items():
            if isinstance(region, int):
                region = SoCRegion(origin=region, size=0x1000000)
            self.add_region(name, region)

        self.logger.info(colorer("Bus Handler created."))

    # Add/Allog/Check Regions ----------------------------------------------------------------------
    def add_region(self, name, region):
        allocated = False
        # Check if SoCIORegion
        if isinstance(region, SoCIORegion):
            if name in self.masters.keys():
                self.logger.error("{} already declared as IO Region:".format(colorer(name, color="red")))
                self.logger.error(self)
                raise
            self.io_regions[name] = region
            overlap = self.check_regions_overlap(self.io_regions)
            if overlap is not None:
                self.logger.error("IO Region overlap between {} and {}:".format(
                    colorer(overlap[0], color="red"),
                    colorer(overlap[1], color="red")))
                self.logger.error(str(self.regions[overlap[0]]))
                self.logger.error(str(self.regions[overlap[1]]))
                raise
            self.logger.info("{} Region {} {}.".format(
                colorer(name,    color="underline"),
                colorer("added", color="green"),
                str(region)))
        # Check if SoCLinkerRegion
        elif isinstance(region, SoCLinkerRegion):
            if name in self.masters.keys():
                self.logger.error("{} already declared as Linker Region:".format(colorer(name, color="red")))
                self.logger.error(self)
                raise
            self.ld_regions[name] = region
            overlap = self.check_regions_overlap(self.ld_regions)
            if overlap is not None:
                self.logger.error("Linker Region overlap between {} and {}:".format(
                    colorer(overlap[0], color="red"),
                    colorer(overlap[1], color="red")))
                self.logger.error(str(self.regions[overlap[0]]))
                self.logger.error(str(self.regions[overlap[1]]))
                raise
            self.logger.info("{} Region {} {}.".format(
                colorer(name,    color="underline"),
                colorer("added", color="green"),
                str(region)))
        # Check if SoCRegion
        elif isinstance(region, SoCRegion):
            # If no origin specified, allocate region.
            if region.origin is None:
                allocated = True
                region    = self.alloc_region(name, region.size, region.cached)
                self.regions[name] = region
            # Else add region and check for overlaps.
            else:
                if not region.cached:
                    if not self.check_region_is_io(region):
                        self.logger.error("{} Region {}: {}".format(
                            colorer(name, color="red"),
                            colorer("not cached but not in IO region", color="red"),
                            str(region)))
                        self.logger.error(self)
                        raise
                self.regions[name] = region
                overlap = self.check_regions_overlap(self.regions)
                if overlap is not None:
                    self.logger.error("Region overlap between {} and {}:".format(
                        colorer(overlap[0], color="red"),
                        colorer(overlap[1], color="red")))
                    self.logger.error(str(self.regions[overlap[0]]))
                    self.logger.error(str(self.regions[overlap[1]]))
                    raise
            self.logger.info("{} Region {} {}.".format(
                colorer(name, color="underline"),
                colorer("allocated" if allocated else "added", color="cyan" if allocated else "green"),
                str(region)))
        else:
            self.logger.error("{} is not a supported Region".format(colorer(name, color="red")))
            raise

    def alloc_region(self, name, size, cached=True):
        self.logger.info("Allocating {} Region of size {}...".format(
            colorer("Cached" if cached else "IO"),
            colorer("0x{:08x}".format(size))))

        # Limit Search Regions
        if cached == False:
            search_regions = self.io_regions
        else:
            search_regions = {"main": SoCRegion(origin=0x00000000, size=2**self.address_width-1)}

        # Iterate on Search_Regions to find a Candidate
        for _, search_region in search_regions.items():
            origin = search_region.origin
            while (origin + size) < (search_region.origin + search_region.size):
                # Create a Candicate.
                candidate = SoCRegion(origin=origin, size=size, cached=cached)
                overlap   = False
                # Check Candidate does not overlap with allocated existing regions
                for _, allocated in self.regions.items():
                    if self.check_regions_overlap({"0": allocated, "1": candidate}) is not None:
                        origin  = allocated.origin + allocated.size
                        overlap = True
                        break
                if not overlap:
                    # If no overlap, the Candidate is selected
                    return candidate

        self.logger.error("Not enough Address Space to allocate Region")
        raise

    def check_regions_overlap(self, regions):
        i = 0
        while i < len(regions):
            n0 =  list(regions.keys())[i]
            r0 = regions[n0]
            for n1 in list(regions.keys())[i+1:]:
                r1 = regions[n1]
                if isinstance(r0, SoCLinkerRegion) or isinstance(r1, SoCLinkerRegion):
                    continue
                if r0.origin >= (r1.origin + r1.size):
                    continue
                if r1.origin >= (r0.origin + r0.size):
                    continue
                return (n0, n1)
            i += 1
        return None

    def check_region_is_in(self, region, container):
        is_in = True
        if not (region.origin >= container.origin):
            is_in = False
        if not ((region.origin + region.size) < (container.origin + container.size)):
            is_in = False
        return is_in

    def check_region_is_io(self, region):
        is_io = False
        for _, io_region in self.io_regions.items():
            if self.check_region_is_in(region, io_region):
                is_io = True
        return is_io

    # Add Master/Slave -----------------------------------------------------------------------------
    def add_adapter(self, name, interface):
        if interface.data_width != self.data_width:
            self.logger.info("{} Bus {} from {}-bit to {}-bit.".format(
                colorer(name),
                colorer("converted", color="cyan"),
                colorer(interface.data_width),
                colorer(self.data_width)))
            new_interface = wishbone.Interface(data_width=self.data_width)
            self.submodules += wishbone.Converter(interface, new_interface)
            return new_interface
        else:
            return interface

    def add_master(self, name=None, master=None):
        if name is None:
            name = "master{:d}".format(len(self.masters))
        if name in self.masters.keys():
            self.logger.error("{} already declared as Bus Master:".format(colorer(name, color="red")))
            self.logger.error(self)
            raise
        master = self.add_adapter(name, master)
        self.masters[name] = master
        self.logger.info("{} {} as Bus Master.".format(
            colorer(name,    color="underline"),
            colorer("added", color="green")))

    def add_slave(self, name=None, slave=None, region=None):
        no_name   = name is None
        no_region = region is None
        if no_name and no_region:
            self.logger.error("Please specify at least {} or {} of Bus Slave".format(
                colorer("name",   color="red"),
                colorer("region", color="red")))
            raise
        if no_name:
            name = "slave{:d}".format(len(self.slaves))
        if no_region:
            region = self.regions.get(name, None)
            if region is None:
                self.logger.error("Unable to find Region {}".format(colorer(name, color="red")))
                raise
        else:
             self.add_region(name, region)
        if name in self.slaves.keys():
            self.logger.error("{} already declared as Bus Slave:".format(colorer(name, color="red")))
            self.logger.error(self)
            raise
        slave = self.add_adapter(name, slave)
        self.slaves[name] = slave
        self.logger.info("{} {} as Bus Slave.".format(
            colorer(name, color="underline"),
            colorer("added", color="green")))

    # Str ------------------------------------------------------------------------------------------
    def __str__(self):
        r = "{}-bit {} Bus, {}GiB Address Space.\n".format(
            colorer(self.data_width), colorer(self.standard), colorer(2**self.address_width/2**30))
        r += "IO Regions: ({})\n".format(len(self.io_regions.keys())) if len(self.io_regions.keys()) else ""
        io_regions = {k: v for k, v in sorted(self.io_regions.items(), key=lambda item: item[1].origin)}
        for name, region in io_regions.items():
           r += colorer(name, color="underline") + " "*(20-len(name)) + ": " + str(region) + "\n"
        r += "Linker Regions: ({})\n".format(len(self.ld_regions.keys())) if len(self.ld_regions.keys()) else ""
        ld_regions = {k: v for k, v in sorted(self.ld_regions.items(), key=lambda item: item[1].origin)}
        for name, region in ld_regions.items():
           r += colorer(name, color="underline") + " "*(20-len(name)) + ": " + str(region) + "\n"
        r += "Bus Regions: ({})\n".format(len(self.regions.keys())) if len(self.regions.keys()) else ""
        regions = {k: v for k, v in sorted(self.regions.items(), key=lambda item: item[1].origin)}
        for name, region in regions.items():
           r += colorer(name, color="underline") + " "*(20-len(name)) + ": " + str(region) + "\n"
        r += "Bus Masters: ({})\n".format(len(self.masters.keys())) if len(self.masters.keys()) else ""
        for name in self.masters.keys():
           r += "- {}\n".format(colorer(name, color="underline"))
        r += "Bus Slaves: ({})\n".format(len(self.slaves.keys())) if len(self.slaves.keys()) else ""
        for name in self.slaves.keys():
           r += "- {}\n".format(colorer(name, color="underline"))
        r = r[:-1]
        return r

# SoCLocHandler --------------------------------------------------------------------------------------

class SoCLocHandler(Module):
    # Creation -------------------------------------------------------------------------------------
    def __init__(self, name, n_locs):
        self.name   = name
        self.locs   = {}
        self.n_locs = n_locs

    # Add ------------------------------------------------------------------------------------------
    def add(self, name, n=None, use_loc_if_exists=False):
        allocated = False
        if not (use_loc_if_exists and name in self.locs.keys()):
            if name in self.locs.keys():
                self.logger.error("{} {} name already used.".format(colorer(name, "red"), self.name))
                self.logger.error(self)
                raise
            if n in self.locs.values():
                self.logger.error("{} {} Location already used.".format(colorer(n, "red"), self.name))
                self.logger.error(self)
                raise
            if n is None:
                allocated = True
                n = self.alloc(name)
            else:
                if n < 0:
                    self.logger.error("{} {} Location should be positive.".format(
                        colorer(n, color="red"),
                        self.name))
                    raise
                if n > self.n_locs:
                    self.logger.error("{} {} Location too high (Up to {}).".format(
                        colorer(n, color="red"),
                        self.name,
                        colorer(self.n_csrs, color="green")))
                    raise
            self.locs[name] = n
        else:
            n = self.locs[name]
        self.logger.info("{} {} {} at Location {}.".format(
            colorer(name, color="underline"),
            self.name,
            colorer("allocated" if allocated else "added", color="cyan" if allocated else "green"),
            colorer(n)))

    # Alloc ----------------------------------------------------------------------------------------
    def alloc(self, name):
        for n in range(self.n_locs):
            if n not in self.locs.values():
                return n
        self.logger.error("Not enough Locations.")
        self.logger.error(self)
        raise

    # Str ------------------------------------------------------------------------------------------
    def __str__(self):
        r = "{} Locations: ({})\n".format(self.name, len(self.locs.keys())) if len(self.locs.keys()) else ""
        locs = {k: v for k, v in sorted(self.locs.items(), key=lambda item: item[1])}
        for name in locs.keys():
           r += "- {}{}: {}\n".format(colorer(name, color="underline"), " "*(20-len(name)), colorer(self.locs[name]))
        return r

# SoCCSRHandler ------------------------------------------------------------------------------------

class SoCCSRHandler(SoCLocHandler):
    supported_data_width    = [8, 32]
    supported_address_width = [14, 15]
    supported_alignment     = [32, 64]
    supported_paging        = [0x800]

    # Creation -------------------------------------------------------------------------------------
    def __init__(self, data_width=32, address_width=14, alignment=32, paging=0x800, reserved_csrs={}):
        SoCLocHandler.__init__(self, "CSR", n_locs=4*2**address_width//paging) # FIXME
        self.logger = logging.getLogger("SoCCSRHandler")
        self.logger.info(colorer("Creating new CSR Handler..."))

        # Check Data Width
        if data_width not in self.supported_data_width:
            self.logger.error("Unsupported data_width: {} supporteds: {:s}".format(
                colorer(data_width, color="red"),
                colorer(", ".join(str(x) for x in self.supported_data_width)), color="green"))
            raise

        # Check Address Width
        if address_width not in self.supported_address_width:
            self.logger.error("Unsupported address_width: {} supporteds: {:s}".format(
                colorer(address_width, color="red"),
                colorer(", ".join(str(x) for x in self.supported_address_width), color="green")))
            raise

        # Check Alignment
        if alignment not in self.supported_alignment:
            self.logger.error("Unsupported alignment: {} supporteds: {:s}".format(
                colorer(alignment, color="red"),
                colorer(", ".join(str(x) for x in self.supported_alignment), color="green")))
            raise
        if data_width > alignment:
            self.logger.error("Alignment ({}) should be >= data_width ({})".format(
                colorer(alignment,  color="red"),
                colorer(data_width, color="red")))
            raise

        # Check Paging
        if paging not in self.supported_paging:
            self.logger.error("Unsupported paging: {} supporteds: {:s}".format(
                colorer(paging, color="red"),
                colorer(", ".join(str(x) for x in self.supported_paging), color="green")))
            raise

        # Create CSR Handler
        self.data_width    = data_width
        self.address_width = address_width
        self.alignment     = alignment
        self.paging        = paging
        self.masters       = {}
        self.regions       = {}
        self.logger.info("{}-bit CSR Bus, {}KiB Address Space, {}B Paging (Up to {} Locations).".format(
            colorer(self.data_width),
            colorer(2**self.address_width/2**10),
            colorer(self.paging),
            colorer(self.n_locs)))

        # Adding reserved CSRs
        self.logger.info("Adding {} CSRs...".format(colorer("reserved")))
        for name, n in reserved_csrs.items():
            self.add(name, n)

        self.logger.info(colorer("CSR Handler created."))

    # Add Master -----------------------------------------------------------------------------------
    def add_master(self, name=None, master=None):
        if name is None:
            name = "master{:d}".format(len(self.masters))
        if name in self.masters.keys():
            self.logger.error("{} already declared as CSR Master:".format(colorer(name, color="red")))
            self.logger.error(self)
            raise
        if master.data_width != self.data_width:
            self.logger.error("{} Master/Handler data_width {} ({} vs {}).".format(
                colorer(name),
                colorer("missmatch"),
                colorer(master.data_width, color="red"),
                colorer(self.data_width,   color="red")))
            raise
        self.masters[name] = master
        self.logger.info("{} {} as CSR Master.".format(
            colorer(name,    color="underline"),
            colorer("added", color="green")))

    # Add Region -----------------------------------------------------------------------------------
    def add_region(self, name, region):
        # FIXME: add checks
        self.regions[name] = region

    # Address map ----------------------------------------------------------------------------------
    def address_map(self, name, memory):
        if memory is not None:
            name = name + "_" + memory.name_override
        if self.locs.get(name, None) is None:
            self.logger.error("Undefined {} CSR.".format(colorer(name, color="red")))
            raise
        return self.locs[name]

    # Str ------------------------------------------------------------------------------------------
    def __str__(self):
        r = "{}-bit CSR Bus, {}KiB Address Space, {}B Paging (Up to {} Locations).\n".format(
            colorer(self.data_width),
            colorer(2**self.address_width/2**10),
            colorer(self.paging),
            colorer(self.n_locs))
        r += SoCLocHandler.__str__(self)
        r = r[:-1]
        return r

# SoCIRQHandler ------------------------------------------------------------------------------------

class SoCIRQHandler(SoCLocHandler):
    # Creation -------------------------------------------------------------------------------------
    def __init__(self, n_irqs=32, reserved_irqs={}):
        SoCLocHandler.__init__(self, "IRQ", n_locs=n_irqs)
        self.logger = logging.getLogger("SoCIRQHandler")
        self.logger.info(colorer("Creating new SoC IRQ Handler..."))

        # Check IRQ Number
        if n_irqs > 32:
            self.logger.error("Unsupported IRQs number: {} supporteds: {:s}".format(
                colorer(n, color="red"), colorer("Up to 32", color="green")))
            raise

        # Create IRQ Handler
        self.logger.info("IRQ Handler (up to {} Locations).".format(colorer(n_irqs)))

        # Adding reserved IRQs
        self.logger.info("Adding {} IRQs...".format(colorer("reserved")))
        for name, n in reserved_irqs.items():
            self.add(name, n)

        self.logger.info(colorer("IRQ Handler created."))

    # Str ------------------------------------------------------------------------------------------
    def __str__(self):
        r ="IRQ Handler (up to {} Locations).\n".format(colorer(self.n_locs))
        r += SoCLocHandler.__str__(self)
        r = r[:-1]
        return r

# SoCController ------------------------------------------------------------------------------------

class SoCController(Module, AutoCSR):
    def __init__(self):
        self._reset      = CSRStorage(1, description="""
            Write a ``1`` to this register to reset the SoC.""")
        self._scratch    = CSRStorage(32, reset=0x12345678, description="""
            Use this register as a scratch space to verify that software read/write accesses
            to the Wishbone/CSR bus are working correctly. The initial reset value of 0x1234578
            can be used to verify endianness.""")
        self._bus_errors = CSRStatus(32, description="""
            Total number of Wishbone bus errors (timeouts) since last reset.""")

        # # #

        # Reset
        self.reset = Signal()
        self.comb += self.reset.eq(self._reset.re)

        # Bus errors
        self.bus_error = Signal()
        bus_errors     = Signal(32)
        self.sync += \
            If(bus_errors != (2**len(bus_errors)-1),
                If(self.bus_error, bus_errors.eq(bus_errors + 1))
            )
        self.comb += self._bus_errors.status.eq(bus_errors)

# SoC ----------------------------------------------------------------------------------------------

class SoC(Module):
    def __init__(self, platform, sys_clk_freq,

        bus_standard         = "wishbone",
        bus_data_width       = 32,
        bus_address_width    = 32,
        bus_timeout          = 1e6,
        bus_reserved_regions = {},

        csr_data_width       = 32,
        csr_address_width    = 14,
        csr_alignment        = 32,
        csr_paging           = 0x800,
        csr_reserved_csrs    = {},

        irq_n_irqs           = 32,
        irq_reserved_irqs    = {},
        ):

        self.logger = logging.getLogger("SoC")
        self.logger.info(colorer("        __   _ __      _  __  ", color="bright"))
        self.logger.info(colorer("       / /  (_) /____ | |/_/  ", color="bright"))
        self.logger.info(colorer("      / /__/ / __/ -_)>  <    ", color="bright"))
        self.logger.info(colorer("     /____/_/\\__/\\__/_/|_|  ", color="bright"))
        self.logger.info(colorer("  Build your hardware, easily!", color="bright"))

        self.logger.info(colorer("-"*80, color="bright"))
        self.logger.info(colorer("Creating new SoC... ({})".format(build_time())))
        self.logger.info(colorer("-"*80, color="bright"))

        # SoC attributes ---------------------------------------------------------------------------
        self.platform     = platform
        self.sys_clk_freq = sys_clk_freq
        self.constants    = {}
        self.csr_regions  = {}

        # SoC Bus Handler --------------------------------------------------------------------------
        self.submodules.bus = SoCBusHandler(
            standard         = bus_standard,
            data_width       = bus_data_width,
            address_width    = bus_address_width,
            timeout          = bus_timeout,
            reserved_regions = bus_reserved_regions,
           )

        # SoC Bus Handler --------------------------------------------------------------------------
        self.submodules.csr = SoCCSRHandler(
            data_width    = csr_data_width,
            address_width = csr_address_width,
            alignment     = csr_alignment,
            paging        = csr_paging,
            reserved_csrs = csr_reserved_csrs,
        )

        # SoC IRQ Handler --------------------------------------------------------------------------
        self.submodules.irq = SoCIRQHandler(
            n_irqs        = irq_n_irqs,
            reserved_irqs = irq_reserved_irqs
        )

        self.logger.info(colorer("-"*80, color="bright"))
        self.logger.info(colorer("Initial SoC:"))
        self.logger.info(colorer("-"*80, color="bright"))
        self.logger.info(self.bus)
        self.logger.info(self.csr)
        self.logger.info(self.irq)
        self.logger.info(colorer("-"*80, color="bright"))

        self.add_config("CLOCK_FREQUENCY", int(sys_clk_freq))

    # SoC Helpers ----------------------------------------------------------------------------------
    def check_if_exists(self, name):
        if hasattr(self, name):
            self.logger.error("{} SubModule already declared.".format(colorer(name, "red")))
            raise

    def add_constant(self, name, value=None):
        name = name.upper()
        if name in self.constants.keys():
            self.logger.error("{} Constant already declared.".format(colorer(name, "red")))
            raise
        self.constants[name] = SoCConstant(value)

    def add_config(self, name, value):
        name = "CONFIG_" + name
        if isinstance(value, str):
            self.add_constant(name + "_" + value)
        else:
            self.add_constant(name, value)

    # SoC Main Components --------------------------------------------------------------------------
    def add_controller(self, name="ctrl"):
        self.check_if_exists(name)
        setattr(self.submodules, name, SoCController())
        self.csr.add(name, use_loc_if_exists=True)

    def add_ram(self, name, origin, size, contents=[], mode="rw"):
        ram_bus = wishbone.Interface(data_width=self.bus.data_width)
        ram     = wishbone.SRAM(size, bus=ram_bus, init=contents, read_only=(mode == "r"))
        self.bus.add_slave(name, ram.bus, SoCRegion(origin=origin, size=size, mode=mode))
        self.check_if_exists(name)
        self.logger.info("RAM {} {} {}.".format(
            colorer(name),
            colorer("added", color="green"),
            self.bus.regions[name]))
        setattr(self.submodules, name, ram)

    def add_rom(self, name, origin, size, contents=[]):
        self.add_ram(name, origin, size, contents, mode="r")

    def add_csr_bridge(self, origin):
        self.submodules.csr_bridge = wishbone2csr.WB2CSR(
            bus_csr       = csr_bus.Interface(
            address_width = self.csr.address_width,
            data_width    = self.csr.data_width))
        csr_size   = 2**(self.csr.address_width + 2)
        csr_region = SoCRegion(origin=origin, size=csr_size, cached=False)
        self.bus.add_slave("csr", self.csr_bridge.wishbone, csr_region)
        self.csr.add_master(name="bridge", master=self.csr_bridge.csr)
        self.add_config("CSR_DATA_WIDTH", self.csr.data_width)
        self.add_config("CSR_ALIGNMENT",  self.csr.alignment)

    def add_cpu(self, name="vexriscv", variant="standard", reset_address=None):
        if name not in cpu.CPUS.keys():
            self.logger.error("{} CPU not supported, supporteds: {}".format(
                colorer(name, color="red"),
                colorer(", ".join(cpu.CPUS.keys()), color="green")))
            raise
        # Add CPU
        self.submodules.cpu = cpu.CPUS[name](self.platform, variant)
        # Add Bus Masters/CSR/IRQs
        if not isinstance(self.cpu, cpu.CPUNone):
            self.cpu.set_reset_address(reset_address)
            for n, cpu_bus in enumerate(self.cpu.buses):
                self.bus.add_master(name="cpu_bus{}".format(n), master=cpu_bus)
            self.add_csr("cpu", use_loc_if_exists=True)
            for name, loc in self.cpu.interrupts.items():
                self.irq.add(name, loc)
            if hasattr(self, "ctrl"):
                self.comb += self.cpu.reset.eq(self.ctrl.reset)
            self.add_config("CPU_RESET_ADDR", reset_address)
        # Update SoC with CPU constraints
        for n, (origin, size) in enumerate(self.cpu.io_regions.items()):
            self.bus.add_region("io{}".format(n), SoCIORegion(origin=origin, size=size, cached=False))
        self.mem_map.update(self.cpu.mem_map) # FIXME
        # Add constants
        self.add_config("CPU_TYPE",    str(name))
        self.add_config("CPU_VARIANT", str(variant.split('+')[0]))

    def add_timer(self, name="timer0"):
        self.check_if_exists(name)
        setattr(self.submodules, name, Timer())
        self.csr.add(name, use_loc_if_exists=True)
        self.irq.add(name, use_loc_if_exists=True)

    # SoC finalization -----------------------------------------------------------------------------
    def do_finalize(self):
        self.logger.info(colorer("-"*80, color="bright"))
        self.logger.info(colorer("Finalized SoC:"))
        self.logger.info(colorer("-"*80, color="bright"))
        self.logger.info(self.bus)
        self.logger.info(self.csr)
        self.logger.info(self.irq)
        self.logger.info(colorer("-"*80, color="bright"))

        # SoC Bus Interconnect ---------------------------------------------------------------------
        bus_masters = self.bus.masters.values()
        bus_slaves  = [(self.bus.regions[n].decoder(), s) for n, s in self.bus.slaves.items()]
        if len(bus_masters) and len(bus_slaves):
            self.submodules.bus_interconnect = wishbone.InterconnectShared(
                masters        = bus_masters,
                slaves         = bus_slaves,
                register       = True,
                timeout_cycles = self.bus.timeout)
            if hasattr(self, "ctrl") and self.bus.timeout is not None:
                self.comb += self.ctrl.bus_error.eq(self.bus_interconnect.timeout.error)

        # SoC CSR Interconnect ---------------------------------------------------------------------
        self.submodules.csr_bankarray = csr_bus.CSRBankArray(self,
            address_map   = self.csr.address_map,
            data_width    = self.csr.data_width,
            address_width = self.csr.address_width,
            alignment     = self.csr.alignment
        )
        if len(self.csr.masters):
            self.submodules.csr_interconnect = csr_bus.InterconnectShared(
                masters = list(self.csr.masters.values()),
                slaves  = self.csr_bankarray.get_buses())

        # Add CSRs regions
        for name, csrs, mapaddr, rmap in self.csr_bankarray.banks:
            self.csr.add_region(name, SoCCSRRegion(
                origin   = (self.bus.regions["csr"].origin + self.csr.paging*mapaddr),
                busword  = self.csr.data_width,
                obj      = csrs))

        # Add Memory regions
        for name, memory, mapaddr, mmap in self.csr_bankarray.srams:
            self.csr.add_region(name + "_" + memory.name_override, SoCCSRRegion(
                origin   = (self.bus.regions["csr"].origin + self.csr.paging*mapaddr),
                busworkd = self.csr.data_width,
                obj      = memory))

        # Sort CSR regions by origin
        self.csr.regions = {k: v for k, v in sorted(self.csr.regions.items(), key=lambda item: item[1].origin)}

        # Add CSRs / Config items to constants
        for name, constant in self.csr_bankarray.constants:
            self.add_constant(name + "_" + constant.name, constant.value.value)

        # SoC CPU Check ----------------------------------------------------------------------------
        if not isinstance(self.cpu, cpu.CPUNone):
            for name in ["rom", "sram"]:
                if name not in list(self.bus.regions.keys()) + list(self.bus.ld_regions.keys()):
                    self.logger.error("CPU needs {} Region to be defined as Bus or Linker Region.".format(
                        colorer(name, color="red")))
                    self.logger.error(self.bus)
                    raise

        # SoC IRQ Interconnect ---------------------------------------------------------------------
        if hasattr(self, "cpu"):
            if hasattr(self.cpu, "interrupt"):
                for name, loc in sorted(self.irq.locs.items()):
                    if name in self.cpu.interrupts.keys():
                        continue
                    if hasattr(self, name):
                        module = getattr(self, name)
                        if not hasattr(module, "ev"):
                            self.logger.error("No EventManager found on {} SubModule".format(
                                colorer(name, color="red")))
                        self.comb += self.cpu.interrupt[loc].eq(module.ev.irq)
                    self.add_constant(name + "_INTERRUPT", loc)

    # SoC build ------------------------------------------------------------------------------------
    def build(self, *args, **kwargs):
        return self.platform.build(self, *args, **kwargs)

# LiteXSoC -----------------------------------------------------------------------------------------

class LiteXSoC(SoC):
    # Add Identifier -------------------------------------------------------------------------------
    def add_identifier(self, name="identifier", identifier="LiteX SoC", with_build_time=True):
        self.check_if_exists(name)
        if with_build_time:
            identifier += " " + build_time()
        setattr(self.submodules, name, Identifier(ident))
        self.csr.add(name + "_mem", use_loc_if_exists=True)

    # Add UART -------------------------------------------------------------------------------------
    def add_uart(self, name, baudrate=115200):
        from litex.soc.cores import uart
        if name in ["stub", "stream"]:
            self.submodules.uart = uart.UART()
            if name == "stub":
                self.comb += self.uart.sink.ready.eq(1)
        elif name == "bridge":
            self.submodules.uart = uart.UARTWishboneBridge(
                pads     = self.platform.request("serial"),
                clk_freq = self.sys_clk_freq,
                baudrate = baudrate)
            self.bus.master(name="uart_bridge", master=self.uart.wishbone)
        elif name == "crossover":
            self.submodules.uart = uart.UARTCrossover()
        else:
            if name == "jtag_atlantic":
                from litex.soc.cores.jtag import JTAGAtlantic
                self.submodules.uart_phy = JTAGAtlantic()
            elif name == "jtag_uart":
                from litex.soc.cores.jtag import JTAGPHY
                self.submodules.uart_phy = JTAGPHY(device=self.platform.device)
            else:
                self.submodules.uart_phy = uart.UARTPHY(
                    pads     = self.platform.request(name),
                    clk_freq = self.sys_clk_freq,
                    baudrate = baudrate)
            self.submodules.uart = ResetInserter()(uart.UART(self.uart_phy))
        self.csr.add("uart_phy", use_loc_if_exists=True)
        self.csr.add("uart", use_loc_if_exists=True)
        self.irq.add("uart", use_loc_if_exists=True)

    # Add SDRAM ------------------------------------------------------------------------------------
    def add_sdram(self, name, phy, module, origin, size=None,
        l2_cache_size           = 8192,
        l2_cache_min_data_width = 128,
        l2_cache_reverse        = True,
        **kwargs):

        # LiteDRAM core ----------------------------------------------------------------------------
        self.submodules.sdram = LiteDRAMCore(
            phy             = phy,
            geom_settings   = module.geom_settings,
            timing_settings = module.timing_settings,
            clk_freq        = self.sys_clk_freq,
            **kwargs)
        self.csr.add("sdram")

        # LiteDRAM port ----------------------------------------------------------------------------
        port = self.sdram.crossbar.get_port()
        port.data_width = 2**int(log2(port.data_width)) # Round to nearest power of 2

        # SDRAM size -------------------------------------------------------------------------------
        sdram_size = 2**(module.geom_settings.bankbits +
                         module.geom_settings.rowbits +
                         module.geom_settings.colbits)*phy.settings.databits//8
        if size is not None:
            sdram_size = min(sdram_size, size)
        self.bus.add_region("main_ram", SoCRegion(origin, sdram_size))

        # SoC [<--> L2 Cache] <--> LiteDRAM --------------------------------------------------------
        if self.cpu.name == "rocket":
            # Rocket has its own I/D L1 cache: connect directly to LiteDRAM when possible.
            if port.data_width == self.cpu.mem_axi.data_width:
                self.logger.info("Matching AXI MEM data width ({})\n".format(port.data_width))
                self.submodules += LiteDRAMAXI2Native(
                    axi          = self.cpu.mem_axi,
                    port         = port,
                    base_address = self.bus.regions["main_ram"].origin)
            else:
                self.logger.info("Converting MEM data width: {} to {} via Wishbone".format(
                    port.data_width,
                    self.cpu.mem_axi.data_width))
                # FIXME: replace WB data-width converter with native AXI converter!!!
                mem_wb  = wishbone.Interface(
                    data_width = self.cpu.mem_axi.data_width,
                    adr_width  = 32-log2_int(self.cpu.mem_axi.data_width//8))
                # NOTE: AXI2Wishbone FSMs must be reset with the CPU!
                mem_a2w = ResetInserter()(axi.AXI2Wishbone(
                    axi          = self.cpu.mem_axi,
                    wishbone     = mem_wb,
                    base_address = 0))
                self.comb += mem_a2w.reset.eq(ResetSignal() | self.cpu.reset)
                self.submodules += mem_a2w
                litedram_wb = wishbone.Interface(port.data_width)
                self.submodules += LiteDRAMWishbone2Native(
                    wishbone     = litedram_wb,
                    port         = port,
                    base_address = origin)
                self.submodules += wishbone.Converter(mem_wb, litedram_wb)
        elif self.with_wishbone:
            # Wishbone Slave SDRAM interface -------------------------------------------------------
            wb_sdram = wishbone.Interface()
            self.bus.add_slave("main_ram", wb_sdram, SoCRegion(origin=origin, size=sdram_size))

            if l2_cache_size != 0:
                # Insert L2 cache inbetween Wishbone bus and LiteDRAM
                l2_cache_size = max(l2_cache_size, int(2*port.data_width/8)) # Use minimal size if lower
                l2_cache_size = 2**int(log2(l2_cache_size))                  # Round to nearest power of 2
                self.add_config("L2_SIZE", l2_cache_size)

                # L2 Cache -------------------------------------------------------------------------
                l2_cache_data_width = max(port.data_width, l2_cache_min_data_width)
                l2_cache = wishbone.Cache(
                    cachesize = l2_cache_size//4,
                    master    = wb_sdram,
                    slave     = wishbone.Interface(l2_cache_data_width),
                    reverse   = l2_cache_reverse)
                # XXX Vivado workaround, Vivado is not able to map correctly our L2 cache.
                from litex.build.xilinx.vivado import XilinxVivadoToolchain
                if isinstance(self.platform.toolchain, XilinxVivadoToolchain):
                    from migen.fhdl.simplify import FullMemoryWE
                    self.submodules.l2_cache = FullMemoryWE()(l2_cache)
                else:
                    self.submodules.l2_cache = l2_cache
                # L2 Cache <--> LiteDRAM bridge ----------------------------------------------------
                self.submodules.wishbone_bridge = LiteDRAMWishbone2Native(self.l2_cache.slave, port)
            else:
                self.add_config("L2_SIZE", l2_cache_size)
                litedram_wb = wishbone.Interface(port.data_width)
                self.submodules += wishbone.Converter(wb_sdram, litedram_wb)
                # Wishbone Slave <--> LiteDRAM bridge ----------------------------------------------
                self.submodules.wishbone_bridge = LiteDRAMWishbone2Native(litedram_wb, port)
