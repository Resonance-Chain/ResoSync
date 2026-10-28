import argparse
import sys


def main():
    parser = argparse.ArgumentParser(prog="SaveSync",
                                     description="Sincronizza i salvataggi dei giochi nel cloud")
    parser.add_argument("--minimized", action="store_true",
                        help="parte ridotto a icona (usato dall'avvio automatico)")
    parser.add_argument("--launch", metavar="GIOCO",
                        help="scarica i salvataggi, avvia il gioco, li ricarica quando chiudi")
    parser.add_argument("--quiet", action="store_true",
                        help="con --launch: nessuna finestra (Steam Deck in modalità gioco)")
    parser.add_argument("--service", action="store_true",
                        help="servizio senza finestre (Steam Deck, avviato da systemd)")
    parser.add_argument("--version", action="store_true", help="mostra la versione ed esce")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="comando del gioco (con Steam: %%command%%)")
    args = parser.parse_args()

    if args.version:
        from savesync import __version__
        print(f"SaveSync {__version__}")
        return

    if args.service:
        from savesync.headless import run as run_service
        sys.exit(run_service())

    if args.launch:
        from savesync.launcher import run
        from savesync.system import in_game_mode
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        sys.exit(run(args.launch, command, quiet=args.quiet or in_game_mode()))

    from savesync.gui import main as gui_main
    gui_main(minimized=args.minimized)


if __name__ == "__main__":
    main()
