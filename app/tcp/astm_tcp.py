"""Raw TCP ASTM E1381/E1394 listener.

Emulates an analyzer-side session on the wire:

    host(LIMS)                         device(mock)
         |------ ENQ ---------------------->|
         |<----- ACK -----------------------|
         |------ STX frame(s) ... --------->|   (host query, e.g. H...Q...L)
         |<----- ACK per frame -------------|   (NAK on a bad frame/checksum)
         |------ EOT ---------------------->|
         |<----- ENQ -----------------------|   device initiates transmission
         |------ ACK ---------------------->|
         |<----- STX frames (results) ------|   (resent on NAK, up to 6 times)
         |<----- EOT -----------------------|

The device answering is the one named in the host's H.10 receiver ID (e.g.
ANA-001); otherwise the first TCP-enabled ASTM device. Results are generated
for the specimen id from the host's Q record (or a generated one).
"""

from __future__ import annotations

import asyncio
import logging

from ..config import ASTM_TCP_HOST, ASTM_TCP_PORT, DEVICES, Device, get_device
from ..protocols import astm as A
from ..simulator import generate_measurement

MAX_SENDS_PER_FRAME = 6  # E1381: after the 6th NAK the sender aborts the transfer

logger = logging.getLogger("mock_lab.astm_tcp")

_sessions_served = 0


async def _read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    """Read one control byte, or one whole <STX>...<LF> frame.

    Returns (first_byte, frame); raises IncompleteReadError on EOF.
    """
    first = await reader.readexactly(1)
    if first[0] != A.STX:
        return first[0], b""
    return A.STX, first + await reader.readuntil(A.LF)


async def send_frames(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                      frames: list[bytes], timeout: float) -> tuple[bool, list[str]]:
    """Sender side of the transfer phase: send each frame and wait for its ACK.

    A NAK makes the frame be resent, up to MAX_SENDS_PER_FRAME times. EOT in
    place of ACK is a receiver interrupt request, which the sender may ignore,
    so it counts as ACK. Returns (all frames accepted, log lines).
    """
    log: list[str] = []
    for fr in frames:
        fn = chr(fr[1])
        for attempt in range(1, MAX_SENDS_PER_FRAME + 1):
            logger.debug("-> frame %s %r", fn, fr)
            writer.write(fr)
            await writer.drain()
            b = await asyncio.wait_for(reader.read(1), timeout=timeout)
            reply = b[0] if b else None
            if reply in (A.ACK, A.EOT):
                retry = f" (attempt {attempt})" if attempt > 1 else ""
                log.append(f"frame {fn} -> {'ACK' if reply == A.ACK else 'EOT'}{retry}")
                break
            if reply != A.NAK:
                log.append(f"frame {fn} -> {'EOF' if reply is None else hex(reply)}")
                return False, log
            log.append(f"frame {fn} -> NAK (attempt {attempt})")
        else:
            return False, log
    return True, log


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    global _sessions_served
    peer = writer.get_extra_info("peername")
    logger.info(f"connection from {peer}")
    try:
        # 1. Handshake: host sends ENQ
        ftype, _ = await asyncio.wait_for(_read_frame(reader), timeout=30.0)
        if ftype != A.ENQ:
            logger.warning(f"expected ENQ, got {hex(ftype)}; closing")
            return
        writer.write(bytes([A.ACK]))
        await writer.drain()
        logger.info("<- ENQ, -> ACK")

        # 2. Host message frames until EOT
        query_buf = bytearray()
        last_fn = None
        while True:
            ftype, frame = await asyncio.wait_for(_read_frame(reader), timeout=30.0)
            if ftype == A.STX:
                try:
                    fn, text, _ = A.parse_frame(frame)
                except ValueError as e:
                    logger.warning(f"bad frame ({e}); -> NAK")
                    writer.write(bytes([A.NAK]))
                    await writer.drain()
                    continue
                logger.debug("<- frame %s %r", fn, text)
                if fn != last_fn:  # the same FN again is a resend of an ACKed frame
                    query_buf += text
                    last_fn = fn
                writer.write(bytes([A.ACK]))
                await writer.drain()
            elif ftype == A.EOT:
                logger.info(f"<- EOT (query had {len(query_buf)} bytes)")
                break
            else:
                logger.warning(f"unexpected control byte {hex(ftype)}; ignoring")

        # 3. Device initiates result transmission
        parsed = A.parse_host_query(bytes(query_buf))
        device = _device_for_connection(parsed["receiver_id"])
        m = generate_measurement(device, parsed["sample_id"])
        recs = A.build_astm_records(device, m)
        frames = A.message_to_frames(recs)

        writer.write(bytes([A.ENQ]))
        await writer.drain()
        ctrl = await asyncio.wait_for(reader.read(1), timeout=10.0)
        if not ctrl or ctrl[0] != A.ACK:
            logger.warning(f"remote did not ACK device ENQ ({ctrl!r}); aborting")
            return

        ok, log = await send_frames(reader, writer, frames, timeout=10.0)
        writer.write(bytes([A.EOT]))  # termination phase, also after an aborted transfer
        await writer.drain()
        if ok:
            _sessions_served += 1
            logger.info(f"session complete: device={device.device_id} "
                        f"sample={m['sample_id']} records={len(recs)} "
                        f"(total sessions served: {_sessions_served})")
        else:
            logger.warning(f"transfer aborted: {log[-1]}")
    except asyncio.IncompleteReadError:
        logger.info("connection closed by peer")
    except asyncio.TimeoutError:
        logger.info("idle timeout; closing connection")
    except Exception as e:  # keep the listener alive no matter what
        logger.error(f"error: {e!r}")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


def _astm_tcp_devices() -> list[Device]:
    return [d for d in DEVICES if d.tcp_enabled and "astm" in d.protocols]


def _device_for_connection(receiver_id: str | None = None) -> Device:
    """Device named by the host's H.10 receiver ID; else the first TCP-enabled ASTM device."""
    candidates = _astm_tcp_devices()
    wanted = get_device(receiver_id) if receiver_id else None
    if wanted in candidates:
        return wanted
    return candidates[0] if candidates else DEVICES[0]


async def start_astm_listener() -> asyncio.AbstractServer:
    server = await asyncio.start_server(handle_client, ASTM_TCP_HOST, ASTM_TCP_PORT)
    ids = ", ".join(d.device_id for d in _astm_tcp_devices())
    logger.info(f"raw ASTM listener on tcp://{ASTM_TCP_HOST}:{ASTM_TCP_PORT} "
                f"(devices by H.10 receiver ID: {ids})")
    return server
