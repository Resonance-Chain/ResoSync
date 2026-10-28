import argparse
import sys


def main():
    parser = argparse.ArgumentParser(prog="SaveSync",
                                     description="Sincronizza i salvataggi dei giochi nel cloud")
    parser.add_argument("--minimized", action="store_true",
                        help="parte ridotto a icona (usato dall'avvio automatico)")
    parser.add_argument("--launch", metavar="GIOCO",
                        help="scarica i salvataggi, avvia il gioco, li ricarica quando chiudi")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="comando del gioco (con Steam: %%command%%)")
    args = parser.parse_args()

    if args.launch:
        from savesync.launcher import run
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        sys.exit(run(args.launch, command))

    from savesync.gui import main as gui_main
    gui_main(minimized=args.minimized)


if __name__ == "__main__":
    main()
