
import argparse

from audio import AudioProcessor
from audioClient import AudioClient

def main():
    parser = argparse.ArgumentParser(description="Stream an audio file to the DAC Writer Daemon.")
    parser.add_argument("audio_file", help="Path to the audio file to process.")
    args = parser.parse_args()

    # Initialize audio processor
    audioProcesser = AudioProcessor(args.audio_file)

    # Initialize audio client 
    client = AudioClient(audioProcesser)

    # Connect to the DAC Writer Daemon
    client.connectToDacWriterDaemon()

    # Start writing audio data to DAC Writer Daemon
    client.processAudio()

if __name__ == "__main__":
    main()