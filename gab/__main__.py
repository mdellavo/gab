import sys
import argparse
import logging

from gab import client, server


log = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(description="gab IRC client")
    parser.add_argument("--debug", action="store_true")
    parser.set_defaults(server=False, client=False)
    subparsers = parser.add_subparsers()

    subparser_server = subparsers.add_parser("server")
    subparser_server.set_defaults(server=True)
    server.build_subparser(subparser_server)

    subparser_client = subparsers.add_parser("client")
    subparser_client.set_defaults(client=True)
    client.build_subparser(subparser_client)

    return parser.parse_args(), parser.print_help


def main():
    args, print_help = parse_args()
    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(level=level, format='%(asctime)s %(message)s')

    if args.server:
        rv = server.main(args)
    elif args.client:
        rv = client.main(args)
    else:
        print_help()
        rv = 1

    return rv

if __name__ == "__main__":
    sys.exit(main())