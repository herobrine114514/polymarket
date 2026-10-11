/** Conservative offline guard for a frozen, fixed-share CTF BUY/FOK intent.
 * This module has no transport, signer, wallet access or submission method.
 */
import assert from 'node:assert/strict';
import {validateWalletIdentity} from './sdk_wallet_identity_guard.mjs';
import {validateOfflineSessionPermission} from './sdk_session_permission_guard.mjs';

const SCALE=1000000n;
const RESERVED=((1n<<64n)-1n)<<40n;
const ZERO='0x'+'00'.repeat(32);
const AMOUNT_DECIMALS=new Map([
  ['100000',3],['10000',4],['5000',5],['2500',6],['1000',5],['100',6],
]);
const EXCHANGE={standard:'0xe111180000d2663c0091e4f400237545b87b996b',
  negativeRisk:'0xe2222d279d744050d28e00520010520000310f59'};
const integer=(value,name)=>{
  assert.equal(typeof value,'string',`${name} must be an integer string`);
  assert.match(value,/^(0|[1-9][0-9]*)$/,`${name} must be unsigned`);
  return BigInt(value);
};
const ceilDiv=(value,divisor)=>(value+divisor-1n)/divisor;
const lower=value=>String(value).toLowerCase();

export function validateOfflineOrder({intent,market,prepared,nowMs,sessionRegistry,localLease}){
  assert.equal(intent.protocol,'ctf-v1','Unsupported asset protocol');
  assert.equal(intent.side,'BUY');assert.equal(intent.orderType,'FOK');
  const order=prepared.order,typed=prepared.typed;
  assert.equal(order.side,'BUY');assert.equal(order.orderType,'FOK');
  assert.equal(order.postOnly===true,false);assert.equal(order.expiration,0);
  const signatureType=intent.signatureType??0;
  assert.equal(order.signatureType,signatureType,'Signature type differs from frozen intent');
  assert.equal(lower(order.maker),lower(intent.funder));
  assert.equal(lower(order.signer),lower(signatureType===3?intent.funder:intent.signer??intent.funder));
  assert.equal(order.builder,ZERO,'Builder fee context unsupported');
  assert.equal(order.metadata,ZERO);
  assert.equal(typed.primaryType,signatureType===3?'TypedDataSign':'Order');
  assert.equal(market.conditionId,intent.conditionId);
  assert.equal(market.protocol,'ctf-v1');assert.equal(market.acceptingOrders,true);
  assert.ok(market.tokens.includes(intent.tokenId),'Market does not contain intended asset');
  assert.equal(order.tokenId,intent.tokenId);
  const asset=integer(intent.tokenId,'asset ID');
  assert.ok(asset<(1n<<256n)&&(asset&RESERVED)!==0n,'Asset namespace conflicts with CTF protocol');
  assert.equal(typeof market.negRisk,'boolean');
  assert.equal(typed.domain.chainId,137);assert.equal(typed.domain.version,'2');
  assert.equal(typed.domain.name,'Polymarket CTF Exchange');
  assert.equal(lower(typed.domain.verifyingContract),market.negRisk?EXCHANGE.negativeRisk:EXCHANGE.standard,
    'Wrong signing exchange');
  assert.ok(Number.isFinite(nowMs));
  assert.ok(Number.isFinite(market.observedAtMs)&&market.observedAtMs<=nowMs&&
    nowMs-market.observedAtMs<=5000,'Stale or future market context');
  assert.ok(Number.isFinite(market.deadlineMs)&&Number.isFinite(market.executionDelayMs)&&
    market.executionDelayMs>=250&&market.executionDelayMs<=120000);
  assert.ok(nowMs+market.executionDelayMs<market.deadlineMs,'Insufficient time before event deadline');
  const stamp=integer(order.timestamp,'order timestamp');
  assert.ok(stamp<=BigInt(Math.trunc(nowMs))&&BigInt(Math.trunc(nowMs))-stamp<=5000n,
    'Stale or future prepared order');
  const shares=integer(intent.sharesMicro,'shares'),minimum=integer(market.minimumSharesMicro,'minimum');
  assert.ok(shares>0n&&minimum>0n&&shares>=minimum,'Below market minimum size');
  assert.equal(shares%10000n,0n,'Share precision would change during SDK encoding');
  assert.equal(integer(order.takerAmount,'encoded shares'),shares,'Encoded quantity differs from frozen intent');
  const price=integer(intent.priceMicro,'price'),tick=integer(market.tickMicro,'tick');
  const decimals=AMOUNT_DECIMALS.get(tick.toString());
  assert.ok(decimals!==undefined,'Unsupported tick');
  assert.ok(price>=tick&&price<=SCALE-tick&&price%tick===0n,'Off-grid limit');
  const quantum=10n**BigInt(6-decimals);
  const expectedMaker=(shares*price/(SCALE*quantum))*quantum;
  assert.equal(integer(order.makerAmount,'encoded collateral'),expectedMaker,
    'Encoded collateral differs from frozen price');
  assert.equal(market.feeExponent,1,'Unsupported fee exponent');
  assert.equal(market.feeTakerOnly,true,'Unsupported fee schedule');
  const rate=integer(market.feeRateMicro,'fee rate');assert.ok(rate<=SCALE);
  // For 0<=r<=1, p+r*p*(1-p) is monotone on [0,1]. The original
  // price cap therefore bounds total BUY collateral plus this fee schedule.
  const reserve=integer(intent.maxCostMicro,'max cost');
  const cashBound=ceilDiv(shares*price,SCALE);
  const feeBound=ceilDiv(shares*rate*price*(SCALE-price),SCALE**3n);
  assert.ok(cashBound+feeBound<=reserve,'All-in cost exceeds frozen reserve');
  const signingMessage=signatureType===3?typed.message.contents:typed.message;
  for(const {name} of typed.types.Order){
    const expected=name==='side'?0:order[name];
    assert.equal(String(signingMessage[name]),String(expected),`Signing payload mismatch: ${name}`);
  }
  const identity=validateWalletIdentity({intent,prepared,market});
  const permission=(intent.signerRole??'owner')==='session'
    ?validateOfflineSessionPermission({intent,market,nowMs,sessionRegistry,localLease}):null;
  return {scope:'offline_only',can_submit:false,encoded_shares:shares.toString(),
    maximum_collateral_micro:cashBound.toString(),maximum_fee_micro:feeBound.toString(),
    maximum_cost_micro:(cashBound+feeBound).toString(),account_reconciliation_required:true,
    cryptographic_signature_verified:identity.recovered_signer_matches_intent,
    cryptographic_signature_validation_required:false,
    session_envelope_verified:identity.session_envelope_verified,
    session_permission_shape_verified:permission?.snapshot_shape_verified??false,
    authenticated_session_authorization_verified:false,
    release_existing_reservations:false,
    onchain_account_validation_required:true,onchain_eip1271_verified:false};
}
