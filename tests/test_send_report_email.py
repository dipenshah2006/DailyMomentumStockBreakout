import smtplib
import unittest
from unittest.mock import Mock, call, patch

import send_report_email as emailer


class SMTPConnectionRetryTests(unittest.TestCase):
    def test_retries_server_disconnect_before_authenticating(self):
        smtp = Mock()
        disconnect = smtplib.SMTPServerDisconnected("Connection unexpectedly closed")

        with patch.object(emailer, "USERNAME", "sender@example.com"), \
                patch.object(emailer, "PASSWORD", "test-app-password"), \
                patch.object(
                    emailer.smtplib,
                    "SMTP_SSL",
                    side_effect=[disconnect, disconnect, smtp],
                ) as smtp_factory, \
                patch.object(emailer.time, "sleep") as sleep:
            result = emailer.connect_smtp_with_retry()

        self.assertIs(result, smtp)
        self.assertEqual(smtp_factory.call_count, 3)
        self.assertEqual(
            smtp_factory.call_args,
            call(
                emailer.SMTP_HOST,
                emailer.SMTP_PORT,
                timeout=emailer.SMTP_CONNECT_TIMEOUT_SECONDS,
            ),
        )
        self.assertEqual(smtp.login.call_args, call("sender@example.com", "test-app-password"))
        self.assertEqual(sleep.call_args_list, [call(1), call(2)])

    def test_authentication_rejection_is_not_retried(self):
        smtp = Mock()
        smtp.login.side_effect = smtplib.SMTPAuthenticationError(535, b"rejected")

        with patch.object(emailer, "USERNAME", "sender@example.com"), \
                patch.object(emailer, "PASSWORD", "bad-app-password"), \
                patch.object(emailer.smtplib, "SMTP_SSL", return_value=smtp) as smtp_factory, \
                patch.object(emailer.time, "sleep") as sleep:
            with self.assertRaises(smtplib.SMTPAuthenticationError):
                emailer.connect_smtp_with_retry()

        smtp_factory.assert_called_once()
        smtp.close.assert_called_once()
        sleep.assert_not_called()

    def test_exhausted_connection_retries_raise_the_last_error(self):
        disconnect = smtplib.SMTPServerDisconnected("Connection unexpectedly closed")

        with patch.object(emailer, "USERNAME", "sender@example.com"), \
                patch.object(emailer, "PASSWORD", "test-app-password"), \
                patch.object(emailer.smtplib, "SMTP_SSL", side_effect=disconnect) as smtp_factory, \
                patch.object(emailer.time, "sleep") as sleep:
            with self.assertRaises(smtplib.SMTPServerDisconnected):
                emailer.connect_smtp_with_retry()

        self.assertEqual(smtp_factory.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(1), call(2)])


if __name__ == "__main__":
    unittest.main()