from email.message import EmailMessage

import dkim
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from boomer_switch.mail_signing import DKIMSigner, MessagePreparationError, configured_signer, message_bytes
from boomer_switch.website_mail import SMTPTransport, SendmailTransport


@pytest.fixture
def signing(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / 'private.pem'
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    path.chmod(0o600)
    import base64
    public = key.public_key().public_bytes(serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo)
    dns_record = b'v=DKIM1; k=rsa; p=' + base64.b64encode(public)
    def dns(name, timeout=5):
        assert name == b'house._domainkey.example.invalid.'
        return dns_record
    return DKIMSigner(domain='example.invalid', selector='house', private_key_path=path), dns, path


def message():
    item = EmailMessage()
    item['From'], item['To'] = 'sender@example.invalid', 'guest@example.invalid'
    item['Subject'], item['Message-ID'] = 'Verification', '<synthetic@localhost>'
    item.set_content('Synthetic verification code.\n.Dot line.\nNon-ASCII: café\n')
    return item


@pytest.mark.parametrize('transport', ['sendmail', 'smtp'])
def test_exact_transport_bytes_verify_and_tampering_fails(signing, monkeypatch, transport):
    signer, dns, _ = signing
    captured = []
    if transport == 'sendmail':
        from pathlib import Path
        def run(args, **kwargs):
            assert args[-2:] == ['--', 'guest@example.invalid']
            captured.append(kwargs['input'])
        monkeypatch.setattr('boomer_switch.website_mail.subprocess.run', run)
        client = SendmailTransport(str(Path('sendmail').resolve()), signer=signer)
    else:
        class SMTP:
            def __init__(self, *args, **kwargs): pass
            def starttls(self, context): pass
            def sendmail(self, sender, recipients, raw):
                assert sender == 'sender@example.invalid' and recipients == ['guest@example.invalid']
                captured.append(raw)
            def close(self): pass
        monkeypatch.setattr('boomer_switch.website_mail.smtplib.SMTP', SMTP)
        client = SMTPTransport('smtp.invalid', 587, None, None, signer=signer)
    client.send(message())
    raw, = captured
    assert b'\n' not in raw.replace(b'\r\n', b'')
    assert dkim.verify(raw, dnsfunc=dns)
    assert not dkim.verify(raw.replace(b'Synthetic verification', b'Changed verification'), dnsfunc=dns)
    assert not dkim.verify(b'From: attacker@example.invalid\r\n'+raw, dnsfunc=dns)


def test_signing_failure_precedes_any_transport_attempt(signing, monkeypatch):
    signer, _, _ = signing
    def forbidden(*args, **kwargs):
        pytest.fail('Transport must not connect after signing failure')
    monkeypatch.setattr('boomer_switch.website_mail.smtplib.SMTP', forbidden)
    item = message()
    item.replace_header('From', 'sender@other.invalid')
    with pytest.raises(MessagePreparationError, match='could not be prepared'):
        SMTPTransport('smtp.invalid', 587, None, None, signer=signer).send(item)


def test_key_validation_and_errors_do_not_disclose_key_or_path(signing, tmp_path):
    signer, _, path = signing
    path.write_bytes(b'PRIVATE_KEY_SENTINEL')
    with pytest.raises(ValueError) as error:
        DKIMSigner(domain='example.invalid', selector='house', private_key_path=path)
    assert 'PRIVATE_KEY_SENTINEL' not in str(error.value) and str(path) not in str(error.value)
    for domain, selector in [('example.invalid\r\n', 'house'), ('example.invalid', 'bad.selector')]:
        with pytest.raises(ValueError):
            DKIMSigner(domain=domain, selector=selector, private_key_path=path)
    # A loaded signer does not reread mutable private key material per message.
    assert b'DKIM-Signature:' in message_bytes(message(), signer)


def test_configured_signer_rejects_domain_mismatch_and_nonprivate_path(signing, tmp_path):
    _, _, path = signing
    config = {'sender':'sender@example.invalid', 'dkim':{'domain':'example.invalid',
        'selector':'house', 'private_key_path':str(path)}}
    assert isinstance(configured_signer(config, tmp_path), DKIMSigner)
    config['sender'] = 'sender@other.invalid'
    with pytest.raises(ValueError, match='match the sender domain'):
        configured_signer(config, tmp_path)
    config['sender'] = 'sender@example.invalid'
    with pytest.raises(ValueError, match='private state'):
        configured_signer(config, tmp_path / 'other')
    assert configured_signer({'sender':'sender@example.invalid'}, tmp_path) is None
