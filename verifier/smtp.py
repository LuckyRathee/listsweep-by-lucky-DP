"""Bounded SMTP probing with a non-existent-recipient control."""
import asyncio
import ipaddress
import secrets
import socket
import time

from .policy import ProbeVerdict, detect_catch_all, interpret_rcpt


class AsyncSMTPProbe:
    """Read SMTP RCPT responses, capped per peer; never issue DATA."""
    def __init__(
        self,
        dns,
        timeout: float = 12.0,
        max_per_host: int = 2,
        min_host_interval: float = 2.0,
        helo_domain: str = 'localhost',
        mail_from: str = 'probe@localhost',
        port: int = 25,
        allow_private: bool = False,
    ):
        self.dns = dns
        self.timeout = timeout
        self.helo_domain = helo_domain
        self.mail_from = mail_from
        self.port = port
        self.allow_private = allow_private
        self._max_per_host = max_per_host
        self._min_host_interval = min_host_interval
        self._locks: dict[str, asyncio.Semaphore] = {}
        self._next_slot: dict[str, float] = {}
        self._guard = asyncio.Lock()
        self._now = time.monotonic

    async def _read(self, reader):
        """Read all lines of one SMTP reply with a hard deadline."""
        lines = []
        while True:
            raw = await asyncio.wait_for(reader.readline(), self.timeout)
            if not raw:
                raise ConnectionError('SMTP peer closed the connection')
            if len(raw) > 8192:
                raise ValueError('SMTP reply line too long')
            line = raw.decode('ascii', errors='replace').rstrip('\r\n')
            lines.append(line)
            if len(line) < 4 or line[3] != '-':
                return lines

    async def _command(self, reader, writer, text: str):
        if '\r' in text or '\n' in text:
            raise ValueError('SMTP command contains a newline')
        writer.write((text + '\r\n').encode('ascii'))
        await asyncio.wait_for(writer.drain(), self.timeout)
        return await self._read(reader)

    async def _transact(self, host: str, addresses: list[str]):
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, self.port, family=socket.AF_UNSPEC), self.timeout
        )
        try:
            greeting = await self._read(reader)
            if not greeting[0].startswith('220'):
                raise ConnectionError('SMTP peer did not offer service')
            ehlo = await self._command(reader, writer, f'EHLO {self.helo_domain}')
            if not ehlo[0].startswith('250'):
                await self._command(reader, writer, f'HELO {self.helo_domain}')
            mail_lines = await self._command(reader, writer, f'MAIL FROM:<{self.mail_from}>')
            mail_code = int(mail_lines[-1][:3]) if mail_lines[-1][:3].isdigit() else 0
            if 400 <= mail_code <= 499:
                raise asyncio.TimeoutError('Temporary reverse-path rejection')
            if not 200 <= mail_code <= 299:
                raise ConnectionError('Reverse path was rejected; recipient is unknown')
            verdicts = []
            for address in addresses:
                lines = await self._command(reader, writer, f'RCPT TO:<{address}>')
                code = int(lines[-1][:3]) if lines[-1][:3].isdigit() else 0
                verdicts.append(interpret_rcpt(code))
            return verdicts
        finally:
            try:
                if not writer.is_closing():
                    writer.write(b'QUIT\r\n')
                    await asyncio.wait_for(writer.drain(), self.timeout)
            except (OSError, RuntimeError, asyncio.TimeoutError, ConnectionError):
                pass
            writer.close()
            await writer.wait_closed()

    async def _acquire(self, host: str):
        async with self._guard:
            lock = self._locks.setdefault(host, asyncio.Semaphore(self._max_per_host))
        await lock.acquire()
        while True:
            async with self._guard:
                now = self._now()
                delay = max(0, self._next_slot.get(host, now) - now)
                if delay <= 0:
                    self._next_slot[host] = now + self._min_host_interval
                    return
            await asyncio.sleep(delay)

    async def check(self, email: str, domain: str, mx_hosts: list[str]):
        """Probe candidate and random control on the same remote host."""
        control = f'evr-{secrets.token_hex(20)}@{domain}'
        resolution_failure = False
        for host in mx_hosts:
            try:
                addresses = await self.dns.resolve_addresses(host)
            except Exception:
                resolution_failure = True
                continue
            if not addresses:
                continue
            if not self.allow_private and any(
                not ipaddress.ip_address(address).is_global for address in addresses
            ):
                return ProbeVerdict('unknown', 'smtp_non_public_target')
            lock = await self._acquire(host)
            try:
                async with asyncio.timeout(self.timeout * 4 + 2):
                    # Connect to the validated numeric address, not a second DNS lookup.
                    pair = await self._transact(addresses[0], [email, control])
            except (OSError, ValueError, ConnectionError, asyncio.TimeoutError):
                continue
            finally:
                self._locks[host].release()
            if pair:
                if pair[0].reason in ('smtp_mailbox_rejected', 'smtp_temporary_failure'):
                    # The candidate already carries its own evidence (a definite rejection
                    # or a temporary failure); the control only matters when it is accepted.
                    verdict = pair[0]
                else:
                    verdict = detect_catch_all(pair[0], pair[1])
                verdict.host = host
                return verdict
        return ProbeVerdict('unknown', 'smtp_unresolved' if resolution_failure else 'smtp_unreachable')
