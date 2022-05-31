#
# Copyright (c) 2022 Airbyte, Inc., all rights reserved.
#

import math
from abc import ABC, abstractmethod
from itertools import chain
from typing import Any, Iterable, Mapping, MutableMapping, Optional

import pendulum
import requests
from airbyte_cdk.models import SyncMode
from airbyte_cdk.sources.streams.http import HttpStream


class StripeStream(HttpStream, ABC):
    url_base = "https://api.stripe.com/v1/"
    primary_key = "id"

    def __init__(self, start_date: int, account_id: str, **kwargs):
        super().__init__(**kwargs)
        self.account_id = account_id
        self.start_date = start_date

    def next_page_token(self, response: requests.Response) -> Optional[Mapping[str, Any]]:
        return None

    def request_params(
        self,
        stream_state: Mapping[str, Any],
        stream_slice: Mapping[str, Any] = None,
        next_page_token: Mapping[str, Any] = None,
    ) -> MutableMapping[str, Any]:

        # Stripe default pagination is 10, max is 100
        params = {"limit": 100}

        # Handle pagination by inserting the next page's token in the request parameters
        if next_page_token:
            params.update(next_page_token)

        return params

    def request_headers(self, **kwargs) -> Mapping[str, Any]:
        if self.account_id:
            return {"Stripe-Account": self.account_id}

        return {}

    def parse_response(self, response: requests.Response, **kwargs) -> Iterable[Mapping]:
        response_json = response.json()
        yield from response_json.get("data", [])  # Stripe puts records in a container array "data"


class IncrementalStripeStream(StripeStream, ABC):
    # Stripe returns most recently created objects first, so we don't want to persist state until the entire stream has been read
    state_checkpoint_interval = math.inf

    def __init__(self, lookback_window_days: int = 0, **kwargs):
        super().__init__(**kwargs)
        self.lookback_window_days = lookback_window_days

    @property
    @abstractmethod
    def cursor_field(self) -> str:
        """
        Defining a cursor field indicates that a stream is incremental, so any incremental stream must extend this class
        and define a cursor field.
        """
        pass

    def get_updated_state(self, current_stream_state: MutableMapping[str, Any], latest_record: Mapping[str, Any]) -> Mapping[str, Any]:
        """
        Return the latest state by comparing the cursor value in the latest record with the stream's most recent state object
        and returning an updated state object.
        """
        return {self.cursor_field: max(latest_record.get(self.cursor_field), current_stream_state.get(self.cursor_field, 0))}

    def request_params(self, stream_state: Mapping[str, Any] = None, **kwargs):
        stream_state = stream_state or {}
        params = super().request_params(stream_state=stream_state, **kwargs)

        start_timestamp = self.get_start_timestamp(stream_state)
        if start_timestamp:
            params["created[gte]"] = start_timestamp
        return params

    def get_start_timestamp(self, stream_state) -> int:
        start_point = self.start_date
        if stream_state and self.cursor_field in stream_state:
            start_point = max(start_point, stream_state[self.cursor_field])

        if start_point and self.lookback_window_days:
            self.logger.info(f"Applying lookback window of {self.lookback_window_days} days to stream {self.name}")
            start_point = int(pendulum.from_timestamp(start_point).subtract(days=abs(self.lookback_window_days)).timestamp())

        return start_point


class Customers(IncrementalStripeStream):
    """
    API docs: https://stripe.com/docs/api/customers/list
    """

    cursor_field = "created"

    def path(self, **kwargs) -> str:
        return "customers"


class BalanceTransactions(IncrementalStripeStream):
    """
    API docs: https://stripe.com/docs/api/balance_transactions/list
    """

    cursor_field = "created"
    name = "balance_transactions"

    def path(self, **kwargs) -> str:
        return "balance_transactions"


class Charges(IncrementalStripeStream):
    """
    API docs: https://stripe.com/docs/api/charges/list
    """

    cursor_field = "created"

    def path(self, **kwargs) -> str:
        return "charges"


class CustomerBalanceTransactions(StripeStream):
    """
    API docs: https://stripe.com/docs/api/customer_balance_transactions/list
    """

    name = "customer_balance_transactions"

    def path(self, stream_slice: Mapping[str, Any] = None, **kwargs):
        customer_id = stream_slice["customer_id"]
        return f"customers/{customer_id}/balance_transactions"

    def read_records(self, stream_slice: Optional[Mapping[str, Any]] = None, **kwargs) -> Iterable[Mapping[str, Any]]:
        customers_stream = Customers(authenticator=self.authenticator, account_id=self.account_id, start_date=self.start_date)
        for customer in customers_stream.read_records(sync_mode=SyncMode.full_refresh):
            yield from super().read_records(stream_slice={"customer_id": customer["id"]}, **kwargs)


class Coupons(IncrementalStripeStream):
    """
    API docs: https://stripe.com/docs/api/coupons/list
    """

    cursor_field = "created"

    def path(self, **kwargs):
        return "coupons"


class Disputes(IncrementalStripeStream):
    """
    API docs: https://stripe.com/docs/api/disputes/list
    """

    cursor_field = "created"

    def path(self, **kwargs):
        return "disputes"


class Events(IncrementalStripeStream):
    """
    API docs: https://stripe.com/docs/api/events/list
    """

    cursor_field = "created"

    def path(self, **kwargs):
        return "events"


class StripeSubStream(StripeStream, ABC):
    """
    Research shows that records related to SubStream can be extracted from Parent streams which already
    contain 1st page of needed items. Thus, it significantly decreases a number of requests needed to get
    all item in parent stream, since parent stream returns 100 items per request.
    Note, in major cases, pagination requests are not performed because sub items are fully reported in parent streams

    For example:
    Line items are part of each 'invoice' record, so use Invoices stream because
    it allows bulk extraction:
        0.1.28 and below - 1 request extracts line items for 1 invoice (+ pagination reqs)
        0.1.29 and above - 1 request extracts line items for 100 invoices (+ pagination reqs)

    if line items object has indication for next pages ('has_more' attr)
    then use current stream to extract next pages. In major cases pagination requests
    are not performed because line items are fully reported in 'invoice' record

    Example for InvoiceLineItems and parent Invoice streams, record from Invoice stream:
        {
          "created": 1641038947,    <--- 'Invoice' record
          "customer": "cus_HezytZRkaQJC8W",
          "id": "in_1KD6OVIEn5WyEQxn9xuASHsD",    <---- value for 'parent_id' attribute
          "object": "invoice",
          "total": 0,
          ...
          "lines": {    <---- sub_items_attr
            "data": [
              {
                "id": "il_1KD6OVIEn5WyEQxnm5bzJzuA",    <---- 'Invoice' line item record
                "object": "line_item",
                ...
              },
              {...}
            ],
            "has_more": false,    <---- next pages from 'InvoiceLineItemsPaginated' stream
            "object": "list",
            "total_count": 2,
            "url": "/v1/invoices/in_1KD6OVIEn5WyEQxn9xuASHsD/lines"
          }
        }
    """

    filter: Optional[Mapping[str, Any]] = None
    add_parent_id: bool = False

    @property
    @abstractmethod
    def parent(self) -> StripeStream:
        """
        :return: parent stream which contains needed records in <sub_items_attr>
        """

    @property
    @abstractmethod
    def parent_id(self) -> str:
        """
        :return: string with attribute name
        """

    @property
    @abstractmethod
    def sub_items_attr(self) -> str:
        """
        :return: string if single primary key, list of strings if composite primary key, list of list of strings if composite primary key consisting of nested fields.
          If the stream has no primary keys, return None.
        """

    def request_params(self, stream_slice: Mapping[str, Any] = None, **kwargs):
        params = super().request_params(stream_slice=stream_slice, **kwargs)

        # add 'starting_after' param
        if not params.get("starting_after") and stream_slice and stream_slice.get("starting_after"):
            params["starting_after"] = stream_slice["starting_after"]

        return params

    def read_records(self, stream_slice: Optional[Mapping[str, Any]] = None, **kwargs) -> Iterable[Mapping[str, Any]]:

        parent_stream = self.parent(authenticator=self.authenticator, account_id=self.account_id, start_date=self.start_date)
        for record in parent_stream.read_records(sync_mode=SyncMode.full_refresh):

            items_obj = record.get(self.sub_items_attr, {})
            if not items_obj:
                continue

            items = items_obj.get("data", [])

            # non-generic filter, mainly for BankAccounts stream only
            if self.filter:
                items = [i for i in items if i.get(self.filter["attr"]) == self.filter["value"]]

            # get next pages
            items_next_pages = []
            if items_obj.get("has_more") and items:
                stream_slice = {self.parent_id: record["id"], "starting_after": items[-1]["id"]}
                items_next_pages = super().read_records(sync_mode=SyncMode.full_refresh, stream_slice=stream_slice, **kwargs)

            for item in chain(items, items_next_pages):
                if self.add_parent_id:
                    # add reference to parent object when item doesn't have it already
                    item[self.parent_id] = record["id"]
                yield item


class Invoices(IncrementalStripeStream):
    """
    API docs: https://stripe.com/docs/api/invoices/list
    """

    cursor_field = "created"

    def path(self, **kwargs):
        return "invoices"


class InvoiceLineItems(StripeSubStream):
    """
    API docs: https://stripe.com/docs/api/invoices/invoice_lines
    """

    name = "invoice_line_items"

    parent = Invoices
    parent_id: str = "invoice_id"
    sub_items_attr = "lines"
    add_parent_id = True

    def path(self, stream_slice: Mapping[str, Any] = None, **kwargs):
        return f"invoices/{stream_slice[self.parent_id]}/lines"


class InvoiceItems(IncrementalStripeStream):
    """
    API docs: https://stripe.com/docs/api/invoiceitems/list
    """

    cursor_field = "date"
    name = "invoice_items"

    def path(self, **kwargs):
        return "invoiceitems"


class Payouts(IncrementalStripeStream):
    """
    API docs: https://stripe.com/docs/api/payouts/list
    """

    cursor_field = "created"

    def path(self, **kwargs):
        return "payouts"


class Plans(IncrementalStripeStream):
    """
    API docs: https://stripe.com/docs/api/plans/list
    """

    cursor_field = "created"

    def path(self, **kwargs):
        return "plans"


class Products(IncrementalStripeStream):
    """
    API docs: https://stripe.com/docs/api/products/list
    """

    cursor_field = "created"

    def path(self, **kwargs):
        return "products"


class Subscriptions(IncrementalStripeStream):
    """
    API docs: https://stripe.com/docs/api/subscriptions/list
    """

    cursor_field = "created"
    status = "all"

    def path(self, **kwargs):
        return "subscriptions"

    def request_params(self, stream_state=None, **kwargs):
        stream_state = stream_state or {}
        params = super().request_params(stream_state=stream_state, **kwargs)
        params["status"] = self.status
        return params


class SubscriptionItems(StripeSubStream):
    """
    API docs: https://stripe.com/docs/api/subscription_items/list
    """

    name = "subscription_items"

    parent: StripeStream = Subscriptions
    parent_id: str = "subscription_id"
    sub_items_attr: str = "items"

    def path(self, **kwargs):
        return "subscription_items"

    def request_params(self, stream_slice: Mapping[str, Any] = None, **kwargs):
        params = super().request_params(stream_slice=stream_slice, **kwargs)
        params["subscription"] = stream_slice[self.parent_id]
        return params


class Transfers(IncrementalStripeStream):
    """
    API docs: https://stripe.com/docs/api/transfers/list
    """

    cursor_field = "created"

    def path(self, **kwargs):
        return "transfers"


class Refunds(IncrementalStripeStream):
    """
    API docs: https://stripe.com/docs/api/refunds/list
    """

    cursor_field = "created"

    def path(self, **kwargs):
        return "refunds"


class PaymentIntents(IncrementalStripeStream):
    """
    API docs: https://stripe.com/docs/api/payment_intents/list
    """

    cursor_field = "created"

    def path(self, **kwargs):
        return "payment_intents"


class BankAccounts(StripeSubStream):
    """
    API docs: https://stripe.com/docs/api/customer_bank_accounts/list
    """

    name = "bank_accounts"

    parent = Customers
    parent_id = "customer_id"
    sub_items_attr = "sources"
    filter = {"attr": "object", "value": "bank_account"}

    def path(self, stream_slice: Mapping[str, Any] = None, **kwargs):
        return f"customers/{stream_slice[self.parent_id]}/sources"

    def request_params(self, stream_slice: Mapping[str, Any] = None, **kwargs) -> MutableMapping[str, Any]:
        params = super().request_params(**kwargs)
        params["object"] = "bank_account"
        return params


class CheckoutSessions(StripeStream):
    """
    API docs: https://stripe.com/docs/api/checkout/sessions/list
    """

    name = "checkout_sessions"

    def path(self, **kwargs):
        return "checkout/sessions"


class CheckoutSessionsLineItems(StripeStream):
    """
    API docs: https://stripe.com/docs/api/checkout/sessions/line_items
    """

    name = "checkout_sessions_line_items"

    def path(self, stream_slice: Mapping[str, Any] = None, **kwargs):
        return f"checkout/sessions/{stream_slice['checkout_session_id']}/line_items"

    def read_records(self, stream_slice: Optional[Mapping[str, Any]] = None, **kwargs) -> Iterable[Mapping[str, Any]]:
        checkout_session_stream = CheckoutSessions(authenticator=self.authenticator, account_id=self.account_id, start_date=self.start_date)
        for checkout_session in checkout_session_stream.read_records(sync_mode=SyncMode.full_refresh):
            yield from super().read_records(stream_slice={"checkout_session_id": checkout_session["id"]}, **kwargs)

    def request_params(self, stream_slice: Mapping[str, Any] = None, **kwargs):
        params = super().request_params(stream_slice=stream_slice, **kwargs)
        params["expand[]"] = ["data.discounts", "data.taxes"]
        return params

    @property
    def raise_on_http_errors(self):
        return False

    def parse_response(self, response: requests.Response, stream_slice: Mapping[str, Any] = None, **kwargs) -> Iterable[Mapping]:
        if response.status_code == 404:
            self.logger.warning(response.json())
            return
        response.raise_for_status()

        response_json = response.json()
        data = response_json.get("data", [])
        if data and stream_slice:
            cs_id = stream_slice.get("checkout_session_id", None)
            for e in data:
                e["checkout_session_id"] = cs_id
        yield from data


class PromotionCodes(IncrementalStripeStream):
    """
    API docs: https://stripe.com/docs/api/promotion_codes/list
    """

    cursor_field = "created"

    def path(self, **kwargs):
        return "promotion_codes"
