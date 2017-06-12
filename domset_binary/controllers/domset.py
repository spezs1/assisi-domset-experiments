#!/usr/bin/env python
# -*- coding: utf-8 -*-

from assisipy import casu

import sys
import time
from threading import Thread, Event
from datetime import datetime
from copy import deepcopy
import json
import csv
from math import exp

class DomsetController(Thread):

    def __init__(self, rtc_file, log=False):

        Thread.__init__(self)

        self.casu = casu.Casu(rtc_file,log=True)
        # Parse rtc file name to get CASU id
        # assumes casu-xxx.rtc file name format
        self.casu_id = int(rtc_file[-7:-4])
        nbg_ids = [int(name[-3:]) for name in self.casu._Casu__neighbors]
        self.nbg_data_buffer = {}
        self._is_master = 1 # master in CASU group calculates new temp ref
        self.group_size = 1 # only counts the neighbours; have to add myself

        for nb in nbg_ids:
            self.group_size += 1
            self.nbg_data_buffer[nb] = []
            if nb < self.casu_id:
                self._is_master = 0
        '''
        if self._is_master == 0:
            master_id = self.casu_id
            master = self.casu
            for nbg in self.casu._Casu__neighbors:
                self.group_size += 1
                nbg_id = int(nbg[-3:])
                if nbg_id < master_id:
                    master = nbg
                    master_id = nbg_id
            self.master = master
        '''

        self._Td = 0.1 # Sample time for sensor readings is 0.1 second
        self._temp_control_freq = 1.0 / 5.0 # Sample frequency for temperature control in seconds is once in 5 seconds
        self.time_start = time.time()
        self._time_length = 2000.0
        self.t_prev = time.time()
        self.stop_flag = Event()
        self.temp_ref = 28.0

        # sensor activity variables - denote bee presence
        self.activeSensors = [0]
        self._sensors_buf_len = 20 # last 2 sec?
        self.ir_thresholds = [25000, 25000, 25000, 25000, 25000, 25000]
        self.integrate_activity = 0.0
        self.average_activity = 0.0
        self.maximum_activity = 0.0
        self.temp_ctrl = 0
        self.initial_heating = 0
        self.heat_float = 0.0
        self.cool_float = 0.0

        # constants for temperature control
        self._integration_limit = 100.0
        self._integrate_limit_lower = 2
        self._integrate_limit_upper = 4
        self._stop_initial_heating = 10
        self._inflection_heat = 0.17
        self._inflection_cool = 0.55
        self._start_heat = 0.0
        self._stop_heat = 0.7
        self._start_cool = 0.1
        self._stop_cool = 0.5
        self._rho = 0.85
        self._step_heat = 0.1
        self._step_cool = 0.07

        # Set up zeta logging
        now_str = datetime.now().__str__().split('.')[0]
        now_str = now_str.replace(' ','-').replace(':','-')
        self.logfile = open(now_str + '-' + self.casu.name() + '-domset.csv','wb')
        self.logger = csv.writer(self.logfile, delimiter=';')

        self.i = 0


    def calibrate_ir_thresholds(self, margin = 500, duration = 10):
        self.casu.set_diagnostic_led_rgb(r=1)

        t_start = time.time()
        count = 0
        ir_raw_buffers = [[0],[0],[0],[0],[0],[0]]
        while time.time() - t_start < duration:
            ir_raw = self.casu.get_ir_raw_value(casu.ARRAY)
            for (val, buff) in zip(ir_raw, ir_raw_buffers):
                buff.append(val)
            time.sleep(0.1)

        self.ir_thresholds = [max(buff)+margin for buff in ir_raw_buffers]
        print(self.casu.name(), self.ir_thresholds)

        self.casu.diagnostic_led_standby()


    def update(self):

        t_old = self.t_prev
        self.t_prev = time.time()
        casu_id = self.casu_id

        if self._is_master:
            # calculate local ir sensor activity over time
            self.calculate_self_average_activity()

            # receive group ir readings
            updated_all = False
            while not updated_all:
                msg = self.casu.read_message()
                if msg:
                    nbg_id = int(msg['sender'][-3:])
                    #print('received message from: ' + str(nbg_id) + ' saying: ' + str(msg['data']))
                    self.nbg_data_buffer[nbg_id].append(msg['data'])
                    #print(self.nbg_data_buffer[nbg_id])
                    # Check if we now have at least one message from each neighbor
                    updated_all = True
                    for nbg in self.nbg_data_buffer:
                        if not self.nbg_data_buffer[nbg]:
                            print('+++ missing ir readings +++ ' + str(nbg))
                            updated_all = False
                        #else:
                            #self.nbg_data_buffer[nbg_id].pop(0)

            # calculate cumulative sensor activity of a group --> temperature control
            self.calculate_sensor_activity()
            self.calculate_temp_ref()
            for nbg in self.casu._Casu__neighbors:
                self.casu.send_message(nbg,json.dumps(self.temp_ref))
        else:
            self.calculate_self_average_activity()

            # send self ir readings to group master
            master_id = casu_id
            master = self.casu
            for nbg in self.casu._Casu__neighbors:
                nbg_id = int(nbg[-3:])
                if nbg_id < master_id:
                    master = nbg
                    master_id = nbg_id
            self.casu.send_message(master,json.dumps(self.average_activity))

            # wait for new temp reference from group master
            updated_temp_ref = False
            while not updated_temp_ref:
                msg = self.casu.read_message()
                if msg:
                    nbg_id = int(msg['sender'][-3:])
                    self.nbg_data_buffer[nbg_id].append(msg['data'])
                    updated_temp_ref = True
            data = self.nbg_data_buffer[nbg_id].pop(0).split(';')
            t_ref = float(json.loads(data[0]))
#            print(t_ref)
            self.temp_ref_old = self.temp_ref
            self.temp_ref = t_ref

        # Set temperature reference
        if not (self.temp_ref_old == self.temp_ref):
            self.casu.set_temp(self.temp_ref)

    def run(self):
        # Just call update every Td
        self.i = 0
        while not self.stop_flag.wait(self._Td):
            self.update_activeSensors_estimate()
            self.i += 1
            if (self.i >= 1 / (self._Td * float(self._temp_control_freq))):
                self.update()
                self.i = 0

    def update_activeSensors_estimate(self):
        """
        Bee density estimator.
        """
        activeSensors_current = [x>t for (x,t) in zip(self.casu.get_ir_raw_value(casu.ARRAY), self.ir_thresholds) if x < 65535]
        if len(activeSensors_current) > 0:
            activeSensors_current_percentage = sum(activeSensors_current) / float(len(activeSensors_current))
            self.activeSensors.append(activeSensors_current_percentage)
        else:
            self.activeSensors.append(-1)
        if len(self.activeSensors) > self._sensors_buf_len:
            self.activeSensors.pop(0)

    def calculate_self_average_activity(self):
        activeSensors = [x for x in self.activeSensors if x >= 0]
        try:
            if len(activeSensors) > 0:
                self.average_activity = sum(activeSensors) / float(len(activeSensors))
            else:
                self.average_activity = -1
        except:
            self.average_activity = -1

    def calculate_sensor_activity(self):
        group_functional = self.group_size

        if self.average_activity == -1:
            self.average_activity = 0
            self.maximum_activity = 0
            group_functional -= 1

#        print('self.group_size ' + str(self.group_size))
#        print('group_functional ' + str(group_functional))
        for nbg in self.nbg_data_buffer:
            data = self.nbg_data_buffer[nbg].pop(0).split(';')
            tmp = json.loads(data[0])
            if not (tmp == -1):
                self.average_activity += tmp
                if self.maximum_activity < tmp:
                    self.maximum_activity = tmp
            else:
                group_functional -= 1

        if self.integrate_activity < self._integration_limit:
            self.integrate_activity += self.average_activity
        if group_functional > 0:
            self.average_activity /= group_functional
        #print('average ' + str(self.average_activity))
        #print(self.maximum_activity)
        #print('integrate ' + str(self.integrate_activity))

    def calculate_temp_ref(self):
        """
        Dominating set temperature contol based on sensor activity of CASU group
        """
        # directly from matlab. Should rewrite clearer
        if (self.integrate_activity > self._integrate_limit_lower) and (self.temp_ctrl < self._stop_initial_heating):
            self.temp_ctrl += 1
        self.initial_heating = 1
        if (self.integrate_activity < self._integrate_limit_lower) or (self.integrate_activity > self._integrate_limit_upper) or (self.temp_ctrl >= self._stop_initial_heating) :
            self.initial_heating = 0

        i_n = (self.t_prev - self.time_start) / self._time_length;
        progress = 1.0 - 1.0 / (1.0 - i_n)
        progress_heat = 1 - exp(self._inflection_heat * progress)
        progress_cool = 1 - exp(self._inflection_cool * progress)
        scaling_heat = (1.0 - progress_heat) * self._start_heat + progress_heat * self._stop_heat
        scaling_cool = (1.0 - progress_cool) * self._start_cool + progress_cool * self._stop_cool

        self.heat_float = (1 - self._rho) * self.heat_float
        if (self.average_activity > scaling_heat) and (self.temp_ctrl > 0) or (self.initial_heating > 1):
             self.heat_float += self._rho * 1.0
        if (self.heat_float > 0.5):
            heat = 1.0
        else:
            heat = 0.0
        self.cool_float = (1.0 - self._rho) * self.cool_float
        if (self.maximum_activity < scaling_cool) and (heat == 0) and (self.temp_ctrl > 0):
            self.cool_float += self._rho * 1.0
        if (self.cool_float > 0.5):
            cool = 1.0
        else:
            cool = 0.0

        d_t_ref = 0.0
        if (heat == 1.0):
            d_t_ref = self._step_heat * self.group_size
        if (cool == 1.0):
            d_t_ref = - self._step_cool
        if (d_t_ref > 0.5):
            d_t_ref = 0.5

        self.temp_ref_old = self.temp_ref
        self.temp_ref = self.temp_ref + d_t_ref
        if self.temp_ref > 36:
            self.temp_ref = 36
        if self.temp_ref < 26:
            self.temp_ref = 26
        if not (self.temp_ref_old == self.temp_ref):
            print('new temperature reference ')


if __name__ == '__main__':

    assert(len(sys.argv) > 1)

    rtc = sys.argv[1]

    # Initialize domset algorithm
    ctrl = DomsetController(rtc, log=True)
    #ctrl.calibrate_ir_thresholds()
    ctrl.start()