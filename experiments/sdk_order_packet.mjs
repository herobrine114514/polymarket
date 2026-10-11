/** Freeze a native SDK synthetic order and its reservation as one replay packet.
 * No credentials, wallet, grant, production DB or external submission method.
 */
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {validateOfflineOrder} from './sdk_order_guard.mjs';
import {validateWalletIdentity} from './sdk_wallet_identity_guard.mjs';

const root=new URL('../',import.meta.url),sdk=new URL('.polycast/oss-polymarket-sdk/node_modules/@polymarket/client/',root);
const requireCrypto=createRequire(new URL('.polycast/oss-chainlink-sdk/package.json',root));
const {TypedDataEncoder,AbiCoder,Interface,keccak256,concat,version}=requireCrypto('ethers');
assert.equal(version,'6.17.0');assert.equal(JSON.parse(readFileSync(new URL('package.json',sdk))).version,'0.12.0');
const SOURCE_PATHS=['experiments/sdk_order_packet.mjs','experiments/sdk_order_guard.mjs',
 'experiments/sdk_wallet_identity_guard.mjs','experiments/sdk_session_permission_guard.mjs',
 '.polycast/oss-polymarket-sdk/package-lock.json'];
const sha=bytes=>createHash('sha256').update(bytes).digest('hex');
const serial=value=>JSON.parse(JSON.stringify(value,(_,v)=>typeof v==='bigint'?v.toString():v));
function sorted(value){
 if(Array.isArray(value))return value.map(sorted);
 if(value&&typeof value==='object')return Object.fromEntries(Object.keys(value).sort().map(k=>[k,sorted(value[k])]));
 return value;
}
export const packetDigest=value=>sha(JSON.stringify(sorted(serial(value))));
const sourceHashes=()=>Object.fromEntries(SOURCE_PATHS.map(p=>[p,sha(readFileSync(new URL(p,root)))]));
const decimal=micro=>{const n=BigInt(micro);return `${n/1000000n}.${(n%1000000n).toString().padStart(6,'0')}`;};
function identifier(value){assert.equal(typeof value,'string');assert.match(value,/^[a-zA-Z0-9:_-]{1,120}$/);return value;}
function exactKeys(value,keys){assert.ok(value&&typeof value==='object'&&!Array.isArray(value));
 assert.deepEqual(Object.keys(value).sort(),[...keys].sort());}

export function deriveContractOrderHash(context){
 const identity=validateWalletIdentity(context),typed=context.prepared.typed,fields=typed.types.Order;
 const typeDescription=TypedDataEncoder.from({Order:fields}).encodeType('Order');
 const typeHash=keccak256(Buffer.from(typeDescription,'utf8'));
 const encoded=AbiCoder.defaultAbiCoder().encode(['bytes32',...fields.map(f=>f.type)],
  [typeHash,...fields.map(f=>identity.contents[f.name])]);
 const structHash=keccak256(encoded),domainHash=TypedDataEncoder.hashDomain(typed.domain);
 assert.equal(structHash,TypedDataEncoder.hashStruct('Order',{Order:fields},identity.contents));
 const orderHash=keccak256(concat(['0x1901',domainHash,structHash]));
 assert.equal(orderHash,TypedDataEncoder.hash(typed.domain,{Order:fields},identity.contents));
 const {EIP712Domain,...signTypes}=typed.types;
 return {contract_order_hash:orderHash,order_struct_hash:structHash,domain_separator:domainHash,
  order_typehash:typeHash,signing_digest:TypedDataEncoder.hash(typed.domain,signTypes,typed.message),
  server_order_identity_verified:false,onchain_hash_verified:false};
}

export function encodeContractHashRead(context){
 const hashes=deriveContractOrderHash(context),order=context.prepared.order,fields=context.prepared.typed.types.Order;
 const iface=new Interface([`function hashOrder((${fields.map(f=>`${f.type} ${f.name}`).join(',')},bytes signature) order) view returns (bytes32)`]);
 const values=fields.map(f=>f.name==='side'?0:order[f.name]);
 return {to:context.prepared.typed.domain.verifyingContract,
  data:iface.encodeFunctionData('hashOrder',[[...values,order.signature]]),expected_result:hashes.contract_order_hash};
}

export async function freezeSyntheticOrderPacket({context,intentId,riskGroup}){
 const checked=validateOfflineOrder(context),hashes=deriveContractOrderHash(context);
 const normalized=serial(context),order=normalized.prepared.order;
 const {postOrder}=await import(new URL('dist/actions/index.js',sdk));
 let body;const stopped=new Error('OFFLINE_PACKET_CAPTURE_STOP');
 await assert.rejects(postOrder({credentials:{key:'SYNTHETIC-ONLY'},secureClob:{post(path,options){
  assert.equal(path,'/order');body=serial(options.json);throw stopped;}}})(order),e=>e===stopped);
 assert.ok(body);assert.equal(body.owner,'SYNTHETIC-ONLY');assert.equal(body.orderType,'FOK');
 assert.equal(body.order.signature,order.signature);
 const reservation={intent_id:identifier(intentId),risk_group:identifier(riskGroup),
  expected_order_id:hashes.contract_order_hash,condition_id:normalized.intent.conditionId,
  token_id:normalized.intent.tokenId,order_type:'FOK',shares:decimal(normalized.intent.sharesMicro),
  limit_price:decimal(normalized.intent.priceMicro),reserve_micro:Number(checked.maximum_cost_micro),
  maximum_fee_micro:Number(checked.maximum_fee_micro),funder:normalized.intent.funder.toLowerCase()};
 assert.ok(Number.isSafeInteger(reservation.reserve_micro)&&Number.isSafeInteger(reservation.maximum_fee_micro));
 const packet={schema:1,scope:'synthetic-native-sdk-signed-order-packet',sdk_version:'0.12.0',
  context:normalized,hashes,reservation,request_object:body,
  request_object_sha256:packetDigest(body),validator_sources:sourceHashes(),can_submit:false,
  real_credentials_used:false,user_wallet_used:false,real_orders_submitted:0};
 return {...packet,packet_sha256:packetDigest(packet)};
}

export async function verifySyntheticOrderPacket(packet,{checkedAtMs}={}){
 exactKeys(packet,['schema','scope','sdk_version','context','hashes','reservation','request_object',
  'request_object_sha256','validator_sources','can_submit','real_credentials_used','user_wallet_used',
  'real_orders_submitted','packet_sha256']);
 assert.equal(packet.schema,1);assert.equal(packet.scope,'synthetic-native-sdk-signed-order-packet');
 assert.equal(packet.sdk_version,'0.12.0');assert.equal(packet.can_submit,false);
 assert.equal(packet.real_credentials_used,false);assert.equal(packet.user_wallet_used,false);assert.equal(packet.real_orders_submitted,0);
 const {packet_sha256,...payload}=packet;assert.equal(packetDigest(payload),packet_sha256,'Packet digest mismatch');
 assert.deepEqual(packet.validator_sources,sourceHashes(),'Packet belongs to different validator sources');
 if(checkedAtMs!==undefined){
  assert.ok(Number.isSafeInteger(checkedAtMs)&&checkedAtMs>0);
  assert.ok(packet.context.nowMs<=checkedAtMs&&checkedAtMs-packet.context.nowMs<=5000,'Packet expired before reservation');
  validateOfflineOrder({...packet.context,nowMs:checkedAtMs});
 }
 const regenerated=await freezeSyntheticOrderPacket({context:packet.context,intentId:packet.reservation.intent_id,
  riskGroup:packet.reservation.risk_group});
 assert.deepEqual(packet,regenerated,'Packet does not bind the signed order and reservation');
 return {scope:'synthetic-packet-validation-only',packet_sha256,contract_order_hash:packet.hashes.contract_order_hash,
  signing_digest:packet.hashes.signing_digest,reservation:packet.reservation,can_submit:false,
  server_order_identity_verified:false,onchain_hash_verified:false};
}

if(process.argv.includes('--verify')){
 try{
  const raw=readFileSync(0,'utf8');assert.ok(Buffer.byteLength(raw)<=100000);
  const {packet,checked_at_ms}=JSON.parse(raw);
  const result=await verifySyntheticOrderPacket(packet,{checkedAtMs:checked_at_ms});console.log(JSON.stringify(result));
 }catch{console.log(JSON.stringify({status:'rejected',reason:'signed_packet_validation_failed',can_submit:false}));process.exitCode=1;}
}
