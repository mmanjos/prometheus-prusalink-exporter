#!/usr/bin/env python3

import argparse
import time
import json
import sys
import logging
import yaml
import requests

import prometheus_client
from prometheus_client.core import GaugeMetricFamily, InfoMetricFamily, REGISTRY
from prometheus_client.registry import Collector

class PrusalinkPrinter:
    def __init__(self, host: str, user: str, password: str, scrape_timeout: int):
        self.host = host
        self.auth = requests.auth.HTTPDigestAuth(user, password)
        self.up = False
        self.scrape_timeout = scrape_timeout

        # PrusaLink-Web API Paths to Scrape
        # See: https://github.com/prusa3d/Prusa-Link-Web/blob/master/spec/openapi.yaml
        self.scrape_paths = {
            "version": "/api/version",
            "status": "/api/v1/status",
            "info": "/api/v1/info",
            "job": "/api/v1/job",
        }

        self.scrape_data = {}
        self.state_metrics = {}
        self.gauge_metrics = {}
        self.info_metrics = {}
        self.labels = {}

    def refresh(self):
        """Rebuild all data for the printer"""
        self._refresh_scrape_data()
        self._set_labels()
        self._update_metrics()

    def _refresh_scrape_data(self):
        """Fetch (HTTP) and Parse (JSON) various api pages off of the printer"""
        # Clear old scrape data
        self.scrape_data = {}

        try:
            for name, path in self.scrape_paths.items():
                response = requests.get(
                    "http://" + self.host + path,
                    auth=self.auth,
                    timeout=self.scrape_timeout,
                )
                if response.status_code == 200:
                    # The response was good; store it
                    self.scrape_data[name] = json.loads(response.content)
                elif response.status_code == 204:
                    # An empty page is still valid for some API calls
                    self.scrape_data[name] = {}
                else:
                    # If any of the api requests have failed, treat the printer as down
                    logging.error( "Unable to fetch %s from %s", path, self.host )
                    logging.error( "Request status code: %s", response.status_code )
                    self.up = False
        except Exception as e:
            logging.error( "Unable to fetch HTTP raw scrape_data on %s", self.host )
            logging.error( "Exception: %s", e )
            self.up = False

        # Only consider the Collector as up if all paths now have data
        if len(self.scrape_data) == len(self.scrape_paths):
            self.up = True

    def _set_labels(self):
        """Set global labels for all metrics relating to this printer"""
        # Clear old lables (needed in case a printer goes offline)
        self.labels = {}
        self.labels["printer"] = self.host
        if self.up:
            self.labels["serialnumber"] = self.scrape_data["info"]["serial"]

    def _update_metrics(self):
        """Place scraped api data into metric data structures so it can be collected"""
        # Clear old metrics
        self.state_metrics = {}
        self.gauge_metrics = {}
        self.info_metrics = {}

        if self.up:
            # Metrics to report on and where to find them in the data

            # Info Metrics

            self.info_metrics = [
                {
                    "name": "prusalink_server_firmware_version",
                    "help": "Prusa Firmware Running on the Printer",
                    "values": {
                        "version": safe_nested_get(self.scrape_data,"Unknown","version","server"),
                        "api": safe_nested_get(self.scrape_data,"Unknown","version","api"),
                    },
                }
            ]

            # State-based Metrics
            # Fake Enum type metrics (no EnumMetricFamily?)
            self.state_metrics = [
                {
                    "name": "prusalink_printer_state",
                    "help": "Current Printer State",
                    "states": [
                        "IDLE",
                        "BUSY",
                        "PRINTING",
                        "PAUSED",
                        "FINISHED",
                        "STOPPED",
                        "ERROR",
                        "ATTENTION",
                        "READY",
                        "UNKNOWN",
                    ],
                    "value": safe_nested_get(self.scrape_data,"UNKNOWN","status","printer","state"),
                }
            ]

            # Gauge Metrics

            self.gauge_metrics = [
                {
                    "name": "prusalink_nozzle_diameter",
                    "help": "Nozzle Diameter in mm",
                    "value": safe_nested_get(self.scrape_data,None,"info","nozzle_diameter"),
                },
                {
                    "name": "prusalink_speed",
                    "help": "Current Printer Configured Speed in Percent",
                    "value": safe_nested_get(self.scrape_data,None,"status","printer","speed"),
                },
                {
                    "name": "prusalink_flow_rate",
                    "help": "Current Printer Configured Flow Rate in Percent",
                    "value": safe_nested_get(self.scrape_data,None,"status","printer","flow"),
                },
                {
                    "name": "prusalink_bed_temp_current",
                    "help": "Current Printer Bed Temperature in Celcius",
                    "value": safe_nested_get(self.scrape_data,None,"status","printer","temp_bed"),
                },
                {
                    "name": "prusalink_bed_temp_desired",
                    "help": "Set (Desired) Printer Bed Temperature in Celcius",
                    "value": safe_nested_get(self.scrape_data,None,"status","printer","target_bed"),
                },
                {
                    "name": "prusalink_nozzle_temp_current",
                    "help": "Current Extruder Nozzle Temperature in Celcius",
                    "value": safe_nested_get(self.scrape_data,None,"status","printer","temp_nozzle"),
                },
                {
                    "name": "prusalink_nozzle_temp_desired",
                    "help": "Set (Desired) Extruder Nozzle Temperature in Celcius",
                    "value": safe_nested_get(self.scrape_data,None,"status","printer","target_nozzle"),
                },
                {
                    "name": "prusalink_axis_z",
                    "help": "Current Z Axis Position in mm",
                    "value": safe_nested_get(self.scrape_data,None,"status","printer","axis_z"),
                },
            ]

            # Extra metrics to add if the printer is working on a job
            stopped_states = ["IDLE", "FINISHED", "STOPPED", "UNKNOWN"]
            if self.scrape_data["status"]["printer"]["state"] not in stopped_states:
                self.gauge_metrics.append(
                    {
                        "name": "prusalink_job_progress",
                        "help": "Current Job Progress in Percent",
                        "value": safe_nested_get(self.scrape_data,None,"job","progress"),
                    }
                )
                self.gauge_metrics.append(
                    {
                        "name": "prusalink_job_time_elapsed",
                        "help": "Current Job Elapsed Time Printing in Seconds",
                        "value": safe_nested_get(self.scrape_data,None,"job","time_printing"),
                    }
                )
                self.gauge_metrics.append(
                    {
                        "name": "prusalink_job_time_remaining",
                        "help": "Current Job Time Remaining in Seconds",
                        "value": safe_nested_get(self.scrape_data,None,"job","time_remaining"),
                    }
                )
                self.info_metrics.append(
                    {
                        "name": "prusalink_job",
                        "help": "Information on the Current Active Job",
                        "values": {
                            "filename": safe_nested_get(self.scrape_data,"Unknown","job","file","display_name"),
                            "filesize": str(safe_nested_get(self.scrape_data,"Unknown","job","file","size")),
                        },
                    }
                )

class PrusalinkCollector(Collector):
    def __init__(self, configdata_printers: list, scrape_timeout: int):
        self.printers = {}
        self.collected_gauge_metrics = {}
        self.collected_state_metrics = {}
        self.collected_info_metrics = {}
        for printer, settings in configdata_printers.items():
            self.printers[str(printer)] = PrusalinkPrinter(
                host=str(printer),
                user=settings["username"],
                password=settings["password"],
                scrape_timeout=scrape_timeout,
            )

    def collect(self):
        """Assemble and yield the scraped metrics"""
        # Reset previously collected metrics
        self.collected_gauge_metrics = {}
        self.collected_state_metrics = {}
        self.collected_info_metrics = {}

        # Always collect this metric
        scrape_successful = GaugeMetricFamily(
            "prusalink_scrape_successful",
            "Indicates if the scrape from the printer was successful",
            labels=["printer", "serialnumber"],
        )

        # Refresh all scrape data, labels and metrics for all printers
        for printer in self.printers.values():
            printer.refresh()

            # Populate the InfoMetricFamily values
            for info_metric in printer.info_metrics:
                info_metric_labels = ["printer", "serialnumber"] + list(
                    info_metric["values"].keys()
                )
                try:
                    self.collected_info_metrics[str(info_metric["name"])]
                except KeyError:
                    self.collected_info_metrics[
                        str(info_metric["name"])
                    ] = InfoMetricFamily(
                        info_metric["name"],
                        info_metric["help"],
                        labels=info_metric_labels,
                    )

                metric_values = dict(printer.labels) | info_metric["values"]
                metric_labels = list(metric_values.keys())
                self.collected_info_metrics[str(info_metric["name"])].add_metric(
                    labels=metric_labels, value=metric_values
                )

            # Populate the GaugeMetricFamily values
            for gauge_metric in printer.gauge_metrics:
                if gauge_metric["value"] is None:
                    # No metric data was found; unable to add_metric
                    continue
                try:
                    self.collected_gauge_metrics[str(gauge_metric["name"])]
                except KeyError:
                    # This metric has not been seen yet; add it to the collected metrics array
                    self.collected_gauge_metrics[
                        str(gauge_metric["name"])
                    ] = GaugeMetricFamily(
                        gauge_metric["name"],
                        gauge_metric["help"],
                        labels=["printer", "serialnumber"],
                    )

                self.collected_gauge_metrics[str(gauge_metric["name"])].add_metric(
                    printer.labels.values(), gauge_metric["value"]
                )

            # Populate the State-based (Enum) values by faking the output with GaugeMetricFamily
            # TODO: Rewrite this using StateSetMetricFamily?
            for state_metric in printer.state_metrics:
                if state_metric["value"] is None:
                    # No metric data was found; unable to add_metric
                    continue
                try:
                    self.collected_state_metrics[str(state_metric["name"])]
                except KeyError:
                    # This metric has not been seen yet; add it to the collected metrics array
                    state_metric_labels = ["printer", "serialnumber", "state"]
                    self.collected_state_metrics[
                        str(state_metric["name"])
                    ] = GaugeMetricFamily(
                        state_metric["name"],
                        state_metric["help"],
                        labels=state_metric_labels,
                    )

                # Add a metric for each state
                for state in state_metric["states"]:
                    state_metric_labels = dict(
                        printer.labels
                    )  # make a copy of the labels for use here
                    state_metric_labels["state"] = state
                    if state == state_metric["value"]:
                        state_metric_value = 1
                    else:
                        state_metric_value = 0
                    self.collected_state_metrics[str(state_metric["name"])].add_metric(
                        state_metric_labels.values(), state_metric_value
                    )

            # Finally, add the overall success metric
            scrape_successful.add_metric(printer.labels.values(), int(printer.up))

        # Send back all of the collected metrics
        yield scrape_successful
        for gauge_metric in self.collected_gauge_metrics.values():
            yield gauge_metric
        for info_metric in self.collected_info_metrics.values():
            yield info_metric
        for state_metric in self.collected_state_metrics.values():
            yield state_metric

def safe_nested_get(d: dict, fallback, *keys):
    """Return the value at dict[keys], or fallback if the key doesn't exist"""
    value = fallback
    try:
        for k in keys:
            d = d[k]
        value = d
    except KeyError:
        logging.warning( "Error finding a value from %s", keys )
    return value

if __name__ == "__main__":
    # Disable extra metrics
    REGISTRY.unregister(prometheus_client.PROCESS_COLLECTOR)
    REGISTRY.unregister(prometheus_client.PLATFORM_COLLECTOR)
    REGISTRY.unregister(prometheus_client.GC_COLLECTOR)

    # Parse command-line args
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config", required=True, help="Config File")
    args = parser.parse_args()

    # Load the config file specified
    with open(args.config, "r",encoding="utf8") as f:
        configdata = yaml.safe_load(f)

    # Default config options
    default_config = {
        "exporter_port": 9528,
        "exporter_address": "127.0.0.1",
        "scrape_timeout": 10,
    }

    # Set default options if they aren't found in the config data
    for setting, default in default_config.items():
        try:
            configdata[setting]
        except KeyError:
            logging.warning(
                "Config setting {0} was not found! Defaulting to: {1}".format(
                    setting, default
                )
            )
            configdata[setting] = default

    # Check that at least the list of printers exists in the loaded config file
    try:
        configdata["printers"]
    except KeyError:
        errormsg = """
Error: no printers were defined in the config file. Nothing to do!
Please make a list of printers in {configpath} following this structure (indentation matters!):
printers:
  "prusaxl.mydomain.invalid":
    username: "maker"
    password: "myprinterpassword"
""".format(
            configpath=args.config
        )
        sys.exit(errormsg)

    # Start the Prometheus Exporter web server
    prometheus_client.start_http_server(
        configdata["exporter_port"], configdata["exporter_address"]
    )

    # Start our collector
    REGISTRY.register(
        PrusalinkCollector(
            configdata["printers"], scrape_timeout=configdata["scrape_timeout"]
        )
    )

    while True:
        time.sleep(1)
