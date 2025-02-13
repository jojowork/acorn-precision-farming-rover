import time
import gps_tools
import datetime


_SUPPORT_NEW_GPS = True
_SUPPORT_OLD_GPS = True

if _SUPPORT_NEW_GPS:
    from serial import Serial
    import pyubx2
    from pyubx2 import UBXReader, itow2utc
    MM_IN_METER = 1000
if _SUPPORT_OLD_GPS:
    import rtk_process


class GPS:
    def __init__(self, logger, simulate_hardware=False, use_new_gps=False):
        self.logger = logger
        self.simulate_hardware = simulate_hardware
        self.simulated_sample = gps_tools.GpsSample(37.353039233,
                                                    -122.333725682, 100,
                                                    ("fix", "fix"), 20, 0,
                                                    time.time(), 0.5)
        self.use_new_gps = use_new_gps
        self.last_gps_sample = None
        self.last_good_gps_sample = None
        self.gps_buffers = ["", ""]
        if self.simulate_hardware:
            self.rtk_socket1 = None
            self.rtk_socket2 = None
        else:
            if not self.use_new_gps:
                rtk_process.launch_rtk_sub_procs(self.logger)
                self.rtk_socket1, self.rtk_socket2 = rtk_process.connect_rtk_procs(self.logger)
            else:
                self.stream_center = Serial('/dev/ttySC4', 921600, timeout=0.10) # Center Receiver
                self.stream_rear = Serial('/dev/ttySC5', 921600, timeout=0.10) # Rear Receiver
                # Set high measurement rate.
                msg = pyubx2.UBXMessage.config_set(layers=1, transaction=0, cfgData=[("CFG_RATE_MEAS", 40)])
                self.stream_center.write(msg.serialize())
                self.stream_rear.write(msg.serialize())
                self.ubr_center = UBXReader(self.stream_center)
                self.ubr_rear = UBXReader(self.stream_rear)
                self.flush_serial()

    def flush_serial(self):
        if self.simulate_hardware:
            return
        self.stream_center.reset_input_buffer()
        self.stream_center.reset_output_buffer()
        self.stream_rear.reset_input_buffer()
        self.stream_rear.reset_output_buffer()

    def last_sample(self):
        """returns the latest GPS sample, either from the GPS device or simulated"""
        return self.simulated_sample if self.simulate_hardware else self.last_gps_sample

    def last_good_sample(self):
        """returns the latest good GPS sample (non-nil), either from the GPS device or simulated"""
        return self.simulated_sample if self.simulate_hardware else self.last_good_gps_sample

    def new_gps_sample(self, print_gps):
        """
        <UBX(NAV-PVAT, iTOW=00:51:08, version=0, validDate=1, validTime=1, fullyResolved=1,
        validMag=1, year=2022, month=4, day=16, hour=0, min=51, sec=8, reserved0=0,
        reserved1=342, tAcc=21, nano=180, fixType=3, gnssFixOK=1, diffSoln=1, vehRollValid=0,
        vehPitchValid=1, vehHeadingValid=1, carrSoln=1, confirmedAvai=1, confirmedDate=1,
        confirmedTime=1, numSV=16, lon=-122.3333403, lat=37.3543208, height=83449,
        hMSL=113754, hAcc=30, vAcc=32, velN=-1, velE=4, velD=0, gSpeed=4, sAcc=134,
        vehRoll=0.0, vehPitch=2.12137, vehHeading=288.11954, motHeading=288.11954,
        accRoll=0.0, accPitch=180.0, accHeading=180.0, magDec=12.86, magAcc=0.7,
        errEllipseOrient=116.64, errEllipseMajor=67, errEllipseMinor=32,
        reserved2=1011181288, reserved3=0)>

        <UBX(NAV-PVT, iTOW=23:23:00.200000, year=2022, month=5, day=25, hour=23,
        min=23, second=0, validDate=1, validTime=1, fullyResolved=1, validMag=0,
        tAcc=21, nano=199625482, fixType=3, gnssFixOk=1, difSoln=1, psmState=0,
        headVehValid=0, carrSoln=2, confirmedAvai=1, confirmedDate=1,
        confirmedTime=1, numSV=18, lon=-122.3333634, lat=37.354267,
        height=83784, hMSL=114090, hAcc=14, vAcc=10, velN=2, velE=-1, velD=-9,
        gSpeed=3, headMot=351.58296, sAcc=16, headAcc=93.05322, pDOP=1.33,
        invalidLlh=0, lastCorrectionAge=2, reserved0=558842204, headVeh=0.0,
        magDec=0.0, magAcc=0.0)>

        0 = no ﬁx
        1 = dead reckoning only
        2 = 2D-ﬁx
        3 = 3D-ﬁx
        4 = GNSS + dead reckoning combined
        5 = time only ﬁx
        """
        data_center = None
        data_rear = None
        try:
            (raw_data_center, data_center) = self.ubr_center.read()
            (raw_data_rear, data_rear) = self.ubr_rear.read()
            if data_center==None or data_rear==None:
                self.logger.error(f"{data_center} {data_rear}")
                return None
            if data_center.identity != "NAV-PVT" or data_rear.identity != "NAV-PVT":
                self.logger.error(f"{data_center} {data_rear}")
                return None

            MAX_REREAD_ATTEMPTS = 5
            reread_attempts = 0
            while data_rear.iTOW != data_center.iTOW:
                if data_center.iTOW > data_rear.iTOW:
                    data_rear = None
                    (raw_data_rear, data_rear) = self.ubr_rear.read()
                elif data_rear.iTOW > data_center.iTOW:
                    data_center = None
                    (raw_data_center, data_center) = self.ubr_center.read()
                reread_attempts += 1
                if reread_attempts > MAX_REREAD_ATTEMPTS:
                    break

            if data_rear.iTOW != data_center.iTOW:
                self.logger.error(f"iTOW mismatch {data_center} {data_rear}")

            if data_center==None or data_rear==None:
                self.logger.error(f"{data_center} {data_rear}")
                return None

            self.raw_samples = (data_center, data_rear)
            microseconds = int(data_center.nano/1000)
            if microseconds < 0:
                microseconds = 0
            time_stamp = datetime.datetime(data_center.year, data_center.month, data_center.day, data_center.hour, data_center.min, data_center.second, microsecond=microseconds, tzinfo=datetime.timezone.utc)
            local_timezone = datetime.datetime.now().astimezone().tzinfo
            time_stamp = time_stamp.replace(tzinfo=datetime.timezone.utc).astimezone(tz=local_timezone)
            heading = gps_tools.get_heading(data_rear, data_center)
            latest_sample = gps_tools.GpsSample(data_center.lat, data_center.lon,
                data_center.hMSL/MM_IN_METER, (data_center.fixType>=3, data_rear.fixType>=3), (
                data_center.numSV, data_rear.numSV), heading, time_stamp, 0)
            # print(f"Speed: {data.gSpeed}, Heading: {data.vehHeading}, Accuracy: {data.accHeading}")
            if print_gps:
                if self.last_gps_sample:
                    period = (latest_sample.time_stamp - self.last_gps_sample.time_stamp).total_seconds()
                else:
                    period = 0.0
                self.logger.info("Lat: {:.8f}, Lon: {:.8f}, Height M: {:.2f}, Heading: {:.1f}, Fix: {}, Period: {:.3f}".format(
                    latest_sample.lat, latest_sample.lon, latest_sample.height_m, latest_sample.azimuth_degrees, latest_sample.status, period))
            return latest_sample
        except Exception as e:
            self.logger.error(e)
            try:
                self.logger.error(f"Center GPS: {data_center}")
                self.logger.error(f"Rear GPS: {data_rear}")
            except:
                pass
            return None

    def update_from_device(self, print_gps, retries):
        """calls the rtk process to get real GPS sample"""
        # print(print_gps)
        if self.use_new_gps:
            self.last_gps_sample = self.new_gps_sample(print_gps)
            if self.last_gps_sample is not None:
                self.last_good_gps_sample = self.last_gps_sample
        else:
            self.gps_buffers, self.last_gps_sample = (
                rtk_process.rtk_loop_once(
                    self.rtk_socket1,
                    self.rtk_socket2,
                    buffers=self.gps_buffers,
                    print_gps=print_gps,
                    last_sample=self.last_gps_sample,
                    retries=retries,
                    logger=self.logger))
        if self.last_gps_sample is not None:
            self.last_good_gps_sample = self.last_gps_sample
            # print(type(self.last_gps_sample.time_stamp))
            # print(self.last_gps_sample)
        else:
            # Occasional bad samples are fine. A very old sample will
            # get flagged in the final checks.
            self.last_gps_sample = self.last_good_gps_sample


    def update_simulated_sample(self, lat, lon, azimuth_degrees):
        """update simulated sample, only useful if the device is simulated"""
        self.simulated_sample = gps_tools.GpsSample(
            lat, lon, self.simulated_sample.height_m, ("fix", "fix"), 20,
            azimuth_degrees, time.time(), 0.5)

    def is_dual_fix(self):
        """check if the last sample is dual fix."""
        if self.use_new_gps:
            try:
                return all(self.last_sample().status)
            except:
                return False
        else:
            return gps_tools.is_dual_fix(self.last_sample())

    def dump(self):
        """dump the sample (only if simulated)"""
        if self.simulate_hardware:
            self.logger.info(
                f"Lat: {self.simulated_sample.lat:.10f}, "
                f"Lon: {self.simulated_sample.lon:.10f}, "
                f"Azimuth: {self.simulated_sample.azimuth_degrees:.2f}, "
                f"Distance: {2.8:.4f}, Fixes: ({True}, {True}), Period: {0.100:.2f}")
