import asyncio
import sys
import os

# Ensure the batmon-ha directory is in the Python path
#sys.path.append(os.path.abspath('/path/to/batmon-ha'))

from bms import BmsSample
from bt import BtBms

def _jbd_command(command: int):
    return bytes([0xDD, 0xA5, command, 0x00, 0xFF, 0xFF - (command - 1), 0x77])

class JbdBt(BtBms):
    UUID_RX = '0000ff01-0000-1000-8000-00805f9b34fb'
    UUID_TX = '0000ff02-0000-1000-8000-00805f9b34fb'
    TIMEOUT = 16

    def __init__(self, address, **kwargs):
        super().__init__(address, **kwargs)
        if kwargs.get('psk'):
            self.logger.warning('JBD usually does not use a pairing PIN')
        self._buffer = bytearray()
        self._switches = None
        self._last_response = None

    def _notification_handler(self, sender, data):
        self._buffer += data
        if self._buffer.endswith(b'w'):
            command = self._buffer[1]
            buf = self._buffer[:]
            self._buffer.clear()
            self._last_response = buf
            self._fetch_futures.set_result(command, buf)

    async def connect(self, **kwargs):
        await super().connect(**kwargs)
        await self.client.start_notify(self.UUID_RX, self._notification_handler)

    async def disconnect(self):
        await self.client.stop_notify(self.UUID_RX)
        await super().disconnect()

    async def _q(self, cmd):
        with self._fetch_futures.acquire(cmd):
            await self.client.write_gatt_char(self.UUID_TX, data=_jbd_command(cmd))
            return await self._fetch_futures.wait_for(cmd, self.TIMEOUT)

    async def fetch(self) -> BmsSample:
        buf = await self._q(cmd=0x03)
        buf = buf[4:]

        num_cell = int.from_bytes(buf[21:22], 'big')
        num_temp = int.from_bytes(buf[22:23], 'big')
        mos_byte = int.from_bytes(buf[20:21], 'big')

        sample = BmsSample(
            voltage=int.from_bytes(buf[0:2], byteorder='big', signed=False) / 100,
            current=-int.from_bytes(buf[2:4], byteorder='big', signed=True) / 100,
            charge=int.from_bytes(buf[4:6], byteorder='big', signed=False) / 100,
            capacity=int.from_bytes(buf[6:8], byteorder='big', signed=False) / 100,
            soc=buf[19],
            num_cycles=int.from_bytes(buf[8:10], byteorder='big', signed=False),
            temperatures=[(int.from_bytes(buf[23 + i * 2:i * 2 + 25], 'big') - 2731) / 10 for i in range(num_temp)],
            switches=dict(
                discharge=mos_byte == 2 or mos_byte == 3,
                charge=mos_byte == 1 or mos_byte == 3,
            ),
        )

        self._switches = dict(sample.switches)
        return sample

    async def fetch_voltages(self):
        buf = await self._q(cmd=0x04)
        num_cell = int(buf[3] / 2)
        voltages = [(int.from_bytes(buf[4 + i * 2:i * 2 + 6], 'big')) for i in range(num_cell)]
        return voltages

    async def set_switch(self, switch: str, state: bool):
        assert switch in {"charge", "discharge"}

        def jbd_checksum(cmd, data):
            crc = 0x10000
            for i in (data + bytes([len(data), cmd])):
                crc = crc - int(i)
            return crc.to_bytes(2, byteorder='big')

        def jbd_message(status_bit, cmd, data):
            return bytes([0xDD, status_bit, cmd, len(data)]) + data + jbd_checksum(cmd, data) + bytes([0x77])

        if not self._switches:
            await self.fetch()

        new_switches = {**self._switches, switch: state}
        switches_sum = sum(new_switches.values())
        if switches_sum == 2:
            tc = 0x00  # all on
        elif switches_sum == 0:
            tc = 0x03  # all off
        elif (switch == "charge" and not state) or (switch == "discharge" and state):
            tc = 0x01  # charge off
        else:
            tc = 0x02  # charge on, discharge off

        data = jbd_message(status_bit=0x5A, cmd=0xE1, data=bytes([0x00, tc]))  # all off
        self.logger.info("send switch msg: %s", data)
        await self.client.write_gatt_char(self.UUID_TX, data=data)

    def debug_data(self):
        return self._last_response

async def main():
    mac_address = input("Enter the MAC address of the BMS: ")
    bms = JbdBt(mac_address, name='jbd')
    await bms.connect()

    while True:
        action = input("Enter 'charge' to enable charging, 'discharge' to enable discharging, or 'exit' to quit: ").strip().lower()
        if action == 'exit':
            break
        elif action in {'charge', 'discharge'}:
            state = input(f"Enter 'on' to enable {action}, 'off' to disable {action}: ").strip().lower() == 'on'
            await bms.set_switch(action, state)
        else:
            print("Invalid input. Please try again.")
        
    await bms.disconnect()

if __name__ == '__main__':
    asyncio.run(main())
