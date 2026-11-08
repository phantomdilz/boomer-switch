"""Serialize one mail message and optionally sign its exact transport bytes."""

from email import policy
from email.parser import BytesParser
from email.utils import formatdate
from email.headerregistry import Address
import os
from pathlib import Path
import re
import stat


class MessagePreparationError(RuntimeError):
    """The message could not be prepared; no transport attempt occurred."""


def configured_signer(mail_config, state_directory):
    """Keep the optional key inside the operator's private state directory."""
    config = mail_config.get('dkim')
    if config is None:
        return None
    if not isinstance(config, dict) or set(config) != {'domain', 'selector', 'private_key_path'}:
        raise ValueError('Configure the DKIM domain, selector, and private key path.')
    try:
        path = Path(config['private_key_path'])
        if not path.is_absolute() or not path.resolve().is_relative_to(Path(state_directory).resolve()):
            raise ValueError
        sender = Address(addr_spec=mail_config['sender'])
        if sender.domain != config['domain']:
            raise ValueError
    except Exception:
        raise ValueError('Keep the DKIM key in private state and match the sender domain.') from None
    return DKIMSigner(**config)


class DKIMSigner:
    """Load one private domain key and use the pinned DKIM implementation."""

    def __init__(self, *, domain, selector, private_key_path):
        if not isinstance(domain,str) or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+',domain):
            raise ValueError("Configure a valid DKIM domain.")
        if not isinstance(selector,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,62}',selector):
            raise ValueError("Configure a valid DKIM selector.")
        path=Path(private_key_path)
        try:
            if not path.is_absolute() or path.is_symlink():
                raise ValueError
            info=path.stat()
            if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= 16384:
                raise ValueError
            if os.name=='posix' and info.st_mode & 0o077:
                raise ValueError
            key=path.read_bytes()
            import dkim
            from dkim.crypto import parse_pem_private_key
            parsed=parse_pem_private_key(key)
            if not 2048 <= parsed['modulus'].bit_length() <= 4096:
                raise ValueError
        except Exception:
            raise ValueError("Read a protected DKIM RSA key from the private state directory.") from None
        self.domain,self.selector,self._key=domain.encode('ascii'),selector.encode('ascii'),key
        self._dkim=dkim

    def sign(self, raw):
        try:
            headers=BytesParser(policy=policy.SMTP).parsebytes(raw,headersonly=True)
            if any(len(headers.get_all(name,[]))!=1 for name in ('From','To','Date','Message-ID')) or headers.get('DKIM-Signature'):
                raise ValueError
            address=headers['From'].addresses
            if len(address)!=1 or address[0].domain.lower().encode('ascii')!=self.domain:
                raise ValueError
            wanted=('from','to','subject','date','message-id','mime-version','content-type','content-transfer-encoding')
            include=[name.encode('ascii') for name in wanted if headers.get(name) is not None]
            # Oversign From so an added second identity invalidates the signature.
            include.append(b'from')
            signature=self._dkim.sign(raw,self.selector,self.domain,self._key,
                signature_algorithm=b'rsa-sha256',canonicalize=(b'relaxed',b'relaxed'),
                include_headers=include,linesep=b'\r\n')
            return signature+raw
        except Exception:
            raise MessagePreparationError("The verification message could not be signed.") from None


def message_bytes(message, signer=None):
    try:
        if message.get('Date') is None:
            message['Date']=formatdate(usegmt=True)
        raw=message.as_bytes(policy=policy.SMTP)
        return signer.sign(raw) if signer is not None else raw
    except Exception:
        raise MessagePreparationError("The verification message could not be prepared.") from None
