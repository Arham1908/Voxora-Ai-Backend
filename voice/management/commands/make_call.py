"""Management command to make outbound calls via Multinet."""

from django.core.management.base import BaseCommand
from voice.sip_client import SIPServer


class Command(BaseCommand):
    help = "Make an outbound call to a phone number (multinet mode only)"

    def add_arguments(self, parser):
        parser.add_argument(
            "to_number",
            type=str,
            help="Phone number to call (e.g. +923001234567)",
        )
        parser.add_argument(
            "--agent",
            type=str,
            default="healthcare",
            help="Agent ID (default: healthcare)",
        )
        parser.add_argument(
            "--voice",
            type=str,
            default="Aoede",
            help="Voice name (default: Aoede)",
        )
        parser.add_argument(
            "--language",
            type=str,
            default="ur-PK",
            help="Language code (default: ur-PK)",
        )
        parser.add_argument(
            "--rtp-port",
            type=int,
            default=12002,
            help="Local RTP port (default: 12002)",
        )

    def handle(self, *args, **options):
        to_number = options["to_number"]
        agent_id = options["agent"]
        voice = options["voice"]
        language = options["language"]
        rtp_port = options["rtp_port"]

        self.stdout.write(self.style.SUCCESS("\n🚀 Initializing SIP Server for outbound call..."))

        try:
            server = SIPServer(agent_id=agent_id, voice=voice, language=language)
            server.start()

            self.stdout.write(self.style.SUCCESS(f"\n📲 Calling: {to_number}"))
            self.stdout.write(f"  Agent: {agent_id}")
            self.stdout.write(f"  Voice: {voice}")
            self.stdout.write(f"  Language: {language}\n")

            # Trigger outbound call
            server.make_outbound_call(to_number, local_rtp_port=rtp_port)

            # Keep running until user interrupts
            try:
                import time
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                self.stdout.write(self.style.WARNING("\n\n⏹  Stopping..."))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"❌ Error: {e}"), ending="\n")
            import traceback
            traceback.print_exc()
        finally:
            try:
                server.stop()
                self.stdout.write(self.style.SUCCESS("✅ SIP Server stopped"))
            except Exception:
                pass
