/** Offline permission-shape checks only; no grant, signer, network or executor.
 * A supplied registry snapshot cannot prove authenticated platform permission.
 * Losing permission blocks new entries and never releases journal reservations.
 */
import assert from 'node:assert/strict';

export const SDK_SESSION_LIFETIME_SECONDS=4315*60*60;
export const MAX_LOCAL_LEASE_MS=30*60*1000;
const lower=value=>String(value).toLowerCase();
function address(value){
 assert.equal(typeof value,'string');assert.match(value,/^0x[0-9a-fA-F]{40}$/);
 assert.notEqual(lower(value),'0x'+'00'.repeat(20));return lower(value);
}
function millis(value){assert.ok(Number.isSafeInteger(value)&&value>0,'Invalid millisecond time');return value;}
function exactKeys(value,keys){
 assert.ok(value&&typeof value==='object'&&!Array.isArray(value));
 assert.deepEqual(Object.keys(value).sort(),[...keys].sort(),'Unexpected permission fields');
}

// Prepare reviewable arguments only. This function cannot call authorizeSessionKey.
// Missing scopes would make SDK 0.12.0 grant ALL; unknown TTL fields are not forwarded.
export function previewSessionAuthorization(request){
 exactKeys(request,['address','scopes']);
 const signer=address(request.address);assert.deepEqual(request.scopes,['CLOB'],'Explicit CLOB-only scope required');
 return {scope:'offline-authorization-argument-preview',sdk_arguments:{address:signer,scopes:['CLOB']},
  platform_lifetime_seconds:SDK_SESSION_LIFETIME_SECONDS,custom_platform_expiry_supported:false,
  deposit_wallet_required:true,builder_credentials_and_beta_allowlist_required:true,
  user_owner_signature_required:true,can_authorize:false,can_submit:false};
}

export function validateOfflineSessionPermission({intent,market,nowMs,sessionRegistry,localLease}){
 assert.equal(intent.signerRole,'session');assert.equal(intent.signatureType,3);
 const signer=address(intent.signer),wallet=address(intent.funder),now=millis(nowMs);
 assert.notEqual(signer,wallet);
 const delay=millis(market.executionDelayMs),finish=now+delay+1000;
 exactKeys(sessionRegistry,['scope','path','account','startedAtMs','receivedAtMs','response']);
 assert.equal(sessionRegistry.scope,'unverified-offline-owner-registry-snapshot');
 assert.equal(sessionRegistry.path,'/v1/user/session-signers');
 exactKeys(sessionRegistry.account,['signer','funder','wallet_type','credential_scope']);
 const account=sessionRegistry.account;
 assert.equal(account.credential_scope,'owner','Owner credentials required for session registry');
 assert.equal(account.wallet_type,3);assert.equal(address(account.funder),wallet);
 const owner=address(account.signer);assert.notEqual(owner,signer);assert.notEqual(owner,wallet);
 const start=millis(sessionRegistry.startedAtMs),received=millis(sessionRegistry.receivedAtMs);
 assert.ok(start<=received&&received<=now&&now-start<=5000,'Stale or future session registry');
 const response=sessionRegistry.response;
 exactKeys(response,['wallet','signers']);assert.equal(address(response.wallet),wallet);
 assert.ok(Array.isArray(response.signers)&&response.signers.length<=2000,'Invalid session list');
 const identifiers=new Set();let selected;
 for(const entry of response.signers){
  exactKeys(entry,['address','scopes','valid_until']);const id=address(entry.address);
  assert.ok(!identifiers.has(id),'Duplicate session signer');identifiers.add(id);
  assert.ok(Array.isArray(entry.scopes)&&entry.scopes.length>0&&entry.scopes.length<=3);
  assert.equal(new Set(entry.scopes).size,entry.scopes.length,'Duplicate session scope');
  assert.ok(entry.scopes.every(s=>['CLOB','COMBOSRFQ','ALL'].includes(s)),'Unsupported session scope');
  assert.ok(!entry.scopes.includes('ALL')||entry.scopes.length===1,'Invalid ALL combination');
  assert.ok(Number.isSafeInteger(entry.valid_until)&&entry.valid_until>0&&entry.valid_until<100000000000,
   'Invalid whole-Unix-second session expiry');
  if(id===signer)selected=entry;
 }
 assert.ok(selected,'Session absent or revoked; new entries denied');
 assert.deepEqual(selected.scopes,['CLOB'],'Selected session scope is broader than this integration');
 assert.ok(selected.valid_until*1000>finish,'Session expiry too near execution');
 exactKeys(localLease,['signer','wallet','createdAtMs','expiresAtMs','paused']);
 assert.equal(address(localLease.signer),signer);assert.equal(address(localLease.wallet),wallet);
 assert.equal(localLease.paused,false,'Local trading paused');
 const created=millis(localLease.createdAtMs),expires=millis(localLease.expiresAtMs);
 assert.ok(created<=now&&expires>finish&&expires-created<=MAX_LOCAL_LEASE_MS,
  'Local lease expired, future, or exceeds 30 minutes');
 assert.ok(expires<=selected.valid_until*1000,'Local lease outlives platform permission');
 return {scope:'offline-session-permission-shape',snapshot_shape_verified:true,
  selected_scope:'CLOB',platform_expiry_unix_seconds:selected.valid_until,
  authenticated_authorization_verified:false,onchain_authorization_verified:false,
  local_lease_revokes_platform_key:false,release_existing_reservations:false,can_submit:false};
}
