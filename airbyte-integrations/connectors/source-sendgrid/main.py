#
# Copyright (c) 2022 Airbyte, Inc., all rights reserved.
#


import sys

from airbyte_cdk.entrypoint import launch
from source_sendgrid.source import SourceSendgrid

if __name__ == "__main__":
    source = SourceSendgrid()
    launch(source, sys.argv[1:])
