import os
import time
from concurrent.futures import Executor, Future
from dataclasses import dataclass
from typing import Optional

import structlog
from eth_typing import ChecksumAddress
from hexbytes import HexBytes
from statemachine.exceptions import TransitionNotAllowed
from web3 import Web3
from web3.constants import ADDRESS_ZERO
from web3.contract import Contract
from web3.types import BlockData

import beamer.metrics
from beamer.config import Config
from beamer.events import (
    ClaimMade,
    ClaimWithdrawn,
    DepositWithdrawn,
    Event,
    FillHashInvalidated,
    HashInvalidated,
    InitiateL1InvalidationEvent,
    InitiateL1ResolutionEvent,
    LatestBlockUpdatedEvent,
    RequestCreated,
    RequestFilled,
    RequestResolved,
)
from beamer.l1_resolution import run_relayer_for_tx
from beamer.models.claim import Claim
from beamer.models.request import Request
from beamer.tracker import Tracker
from beamer.typing import ChainId, ClaimId, FillHash, RequestHash, RequestId
from beamer.util import TokenMatchChecker

log = structlog.get_logger(__name__)


@dataclass
class Context:
    requests: Tracker[RequestId, Request]
    claims: Tracker[ClaimId, Claim]
    request_manager: Contract
    fill_manager: Contract
    match_checker: TokenMatchChecker
    address: ChecksumAddress
    latest_blocks: dict[ChainId, BlockData]
    config: Config
    web3_l1: Web3
    task_pool: Executor
    l1_resolutions: dict[tuple[RequestId, ClaimId], Future]


HandlerResult = tuple[bool, Optional[list[Event]]]


def process_event(event: Event, context: Context) -> HandlerResult:
    log.debug("Processing event", _event=event)

    if isinstance(event, LatestBlockUpdatedEvent):
        return _handle_latest_block_updated(event, context)

    elif isinstance(event, RequestCreated):
        return _handle_request_created(event, context)

    elif isinstance(event, RequestFilled):
        return _handle_request_filled(event, context)

    elif isinstance(event, DepositWithdrawn):
        return _handle_deposit_withdrawn(event, context)

    elif isinstance(event, ClaimMade):
        return _handle_claim_made(event, context)

    elif isinstance(event, ClaimWithdrawn):
        return _handle_claim_withdrawn(event, context)

    elif isinstance(event, RequestResolved):
        return _handle_request_resolved(event, context)

    elif isinstance(event, HashInvalidated):
        return _handle_hash_invalidated(event, context)

    elif isinstance(event, FillHashInvalidated):
        return _handle_fill_hash_invalidated(event, context)

    elif isinstance(event, InitiateL1ResolutionEvent):
        return _handle_initiate_l1_resolution(event, context)

    elif isinstance(event, InitiateL1InvalidationEvent):
        return _handle_initiate_l1_invalidation(event, context)

    else:
        raise RuntimeError("Unrecognized event type")


def _find_claims_by_fill_hash(context: Context, fill_hash: FillHash) -> list[Claim]:
    """
    This returns a list, as there can be multiple claims with the same fill hash
    """
    matching_claims = []
    for claim in context.claims:
        request = context.requests.get(claim.request_id)
        assert request is not None

        if fill_hash == request.fill_hash_with_fill_id(claim.fill_id):
            matching_claims.append(claim)

    return matching_claims


def _find_request_by_request_hash(
    context: Context, request_hash: RequestHash
) -> Optional[Request]:
    for request in context.requests:
        if request.request_hash == request_hash:
            return request
    return None


def _handle_latest_block_updated(
    event: LatestBlockUpdatedEvent, context: Context
) -> HandlerResult:
    context.latest_blocks[event.chain_id] = event.block_data
    return True, None


def _handle_request_created(event: RequestCreated, context: Context) -> HandlerResult:
    with beamer.metrics.update() as data:
        data.requests_created.inc()

    # If `BEAMER_ALLOW_UNLISTED_PAIRS` is set, do not check token match file
    if os.environ.get("BEAMER_ALLOW_UNLISTED_PAIRS") is not None:
        # Check if the address points to some contract
        if context.fill_manager.web3.eth.get_code(event.target_token_address) == HexBytes("0x"):
            log.info(
                "Request unfillable, invalid token contract",
                request_event=event,
                token_address=event.target_token_address,
            )
            return True, None
    else:
        is_valid_request = context.match_checker.is_valid_pair(
            event.chain_id,
            event.source_token_address,
            event.target_chain_id,
            event.target_token_address,
        )

        if not is_valid_request:
            log.debug("Invalid token pair in request", _event=event)
            return True, None

    request = Request(
        request_id=event.request_id,
        source_chain_id=event.chain_id,
        target_chain_id=event.target_chain_id,
        source_token_address=event.source_token_address,
        target_token_address=event.target_token_address,
        target_address=event.target_address,
        amount=event.amount,
        valid_until=event.valid_until,
    )
    context.requests.add(request.id, request)
    return True, None


def _handle_request_filled(event: RequestFilled, context: Context) -> HandlerResult:
    with beamer.metrics.update() as data:
        data.requests_filled.inc()
        if event.filler == context.address:
            data.requests_filled_by_agent.inc()

    request = context.requests.get(event.request_id)
    if request is None:
        return False, None

    fill_matches_request = (
        request.id == event.request_id
        and request.amount == event.amount
        and request.source_chain_id == event.source_chain_id
        and request.target_token_address == event.target_token_address
    )
    if not fill_matches_request:
        log.warn("Fill not matching request. Ignoring.", request=request, fill=event)
        return True, None

    try:
        request.fill(filler=event.filler, fill_tx=event.tx_hash, fill_id=event.fill_id)
    except TransitionNotAllowed:
        return False, None

    log.info("Request filled", request=request)
    return True, None


def _handle_deposit_withdrawn(event: DepositWithdrawn, context: Context) -> HandlerResult:
    request = context.requests.get(event.request_id)
    if request is None:
        return False, None

    try:
        request.withdraw()
    except TransitionNotAllowed:
        return False, None

    log.info("Deposit withdrawn", request=request)
    return True, None


def _handle_claim_made(event: ClaimMade, context: Context) -> HandlerResult:
    # RequestCreated event must arrive before ClaimMade
    # Additionally, a request should never be dropped before all claims are finalized
    request = context.requests.get(event.request_id)
    assert request is not None, "Request object missing upon ClaimMade event"

    claim = context.claims.get(event.claim_id)
    if claim is None:
        challenge_back_off_timestamp = int(time.time())
        # if fill event is not fetched yet, wait `_fill_wait_time`
        # to give the target chain time to sync before challenging
        # additionally, if we are already in the challenge game, no need to back off
        if request.filler is None and event.challenger_stake_total == 0:
            challenge_back_off_timestamp += context.config.fill_wait_time
        claim = Claim(event, challenge_back_off_timestamp)
        context.claims.add(claim.id, claim)

        return True, None

    # Don't process challenge events before there was a chance to invalidate the claim
    if claim.is_started:
        return False, None

    events: list[Event] = []
    if not claim.is_invalidated_l1_resolved and claim.invalidation_tx is not None:
        events.append(
            InitiateL1InvalidationEvent(
                chain_id=request.target_chain_id,  # Resolution happens on the target chain
                claim_id=claim.id,
            )
        )

    # this is at least the second ClaimMade event for this claim id
    assert event.last_challenger != ADDRESS_ZERO, "Second ClaimMade event must contain challenger"
    try:
        claim.challenge(event)
    except TransitionNotAllowed:
        return False, events

    if not request.is_l1_resolved and request.fill_tx is not None:
        events.append(
            InitiateL1ResolutionEvent(
                chain_id=request.target_chain_id,  # Resolution happens on the target chain
                request_id=request.id,
                claim_id=claim.id,
            )
        )

    log.info("Request claimed", request=request, claim_id=event.claim_id)
    return True, events


def _handle_claim_withdrawn(event: ClaimWithdrawn, context: Context) -> HandlerResult:
    claim = context.claims.get(event.claim_id)

    # Check if claim exists, it could happen that we ignored the request because of an
    # invalid token pair, and therefore also did not create the claim
    if claim is None:
        return False, None

    claim.withdraw()
    return True, None


def _handle_request_resolved(event: RequestResolved, context: Context) -> HandlerResult:
    request = _find_request_by_request_hash(context, event.request_hash)
    if request is not None:
        request.l1_resolve(event.filler)
        return True, None

    return False, None


def _handle_hash_invalidated(event: HashInvalidated, context: Context) -> HandlerResult:
    claims = _find_claims_by_fill_hash(context, event.fill_hash)
    for claim in claims:
        claim.start_challenge(event.tx_hash)

    return len(claims) > 0, None


def _handle_fill_hash_invalidated(event: FillHashInvalidated, context: Context) -> HandlerResult:
    claims = _find_claims_by_fill_hash(context, event.fill_hash)
    for claim in claims:
        claim.l1_invalidate()

    return True, None


def _l1_resolution_criteria_fulfilled(claim: Claim, context: Context) -> bool:
    l1_gas_cost = 1_000_000  # TODO: Adapt to real price
    l1_gas_price = context.web3_l1.eth.gas_price
    l1_safety_factor = 1.25
    limit = int(l1_gas_cost * l1_gas_price * l1_safety_factor)

    # Agent is claimer
    if claim.claimer == context.address:
        if claim.latest_claim_made.challenger_stake_total > limit:
            return True
    else:
        # Agent is challenger
        reward = claim.get_challenger_stake(context.address)
        last_challenger = claim.latest_claim_made.last_challenger
        if last_challenger == context.address:
            reward -= (
                claim.latest_claim_made.challenger_stake_total
                - claim.latest_claim_made.claimer_stake
            )

        if reward > limit:
            return True
    return False


def _handle_initiate_l1_resolution(
    event: InitiateL1ResolutionEvent, context: Context
) -> HandlerResult:
    request = context.requests.get(event.request_id)
    claim = context.claims.get(event.claim_id)
    if claim is None:
        return False, None

    # A request should never be dropped before all claims are finalized
    assert request is not None, "Request object missing"

    assert request.fill_tx is not None, "Request not yet filled"
    if _l1_resolution_criteria_fulfilled(claim, context):
        future = context.task_pool.submit(
            run_relayer_for_tx,
            context.config.l1_rpc_url,
            context.config.l2b_rpc_url,
            context.config.account.key,
            request.fill_tx,
        )

        def on_future_done(f: Future) -> None:
            try:
                f.result()
                assert request is not None, "Request object missing"
                assert claim is not None, "Claim object missing"

                del context.l1_resolutions[(request.id, claim.id)]
            except Exception as ex:
                log.error("L1 resolution failed", ex=ex)

        future.add_done_callback(on_future_done)
        context.l1_resolutions[(request.id, claim.id)] = future
        request.l1_resolve()

        log.info("Initiated L1 resolution", request=request, claim=claim)

    return True, None


def _handle_initiate_l1_invalidation(
    event: InitiateL1InvalidationEvent, context: Context
) -> HandlerResult:
    claim = context.claims.get(event.claim_id)
    if claim is None:
        return False, None

    # A request should never be dropped before all claims are finalized
    request = context.requests.get(claim.request_id)
    assert request is not None, "Request object missing"

    assert claim.invalidation_tx is not None, "Claim not invalidated"
    if _l1_resolution_criteria_fulfilled(claim, context):
        future = context.task_pool.submit(
            run_relayer_for_tx,
            context.config.l1_rpc_url,
            context.config.l2b_rpc_url,
            context.config.account.key,
            claim.invalidation_tx,
        )

        def on_future_done(f: Future) -> None:
            try:
                f.result()
                assert request is not None, "Request object missing"
                assert claim is not None, "Claim object missing"

                del context.l1_resolutions[(request.id, claim.id)]
            except Exception as ex:
                log.error("L1 invalidation failed", ex=ex)

        future.add_done_callback(on_future_done)
        context.l1_resolutions[(request.id, claim.id)] = future
        claim.l1_invalidate()

        log.info("Initiated L1 invalidation", request=request, claim=claim)

    return True, None
