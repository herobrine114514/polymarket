/** Pinned CTF-v1 signature envelopes. No wallet access, RPC or submission.
 * Recovering a session signer never proves a live delegation.
 */
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const cryptoRequire=createRequire(new URL('../.polycast/oss-chainlink-sdk/package.json',import.meta.url));
const {AbiCoder,TypedDataEncoder,verifyTypedData,version}=cryptoRequire('ethers');
assert.equal(version,'6.17.0');

const ZERO='0x'+'00'.repeat(32);
const SESSION_MAGIC='6492649264926492649264926492649264926492649264926492649264926492';
const SESSION_ABI=['bytes32','bytes32','bytes'];
const ORDER_FIELDS=[['salt','uint256'],['maker','address'],['signer','address'],['tokenId','uint256'],
 ['makerAmount','uint256'],['takerAmount','uint256'],['side','uint8'],['signatureType','uint8'],
 ['timestamp','uint256'],['metadata','bytes32'],['builder','bytes32']].map(([name,type])=>({name,type}));
const DOMAIN_FIELDS=[['name','string'],['version','string'],['chainId','uint256'],
 ['verifyingContract','address']].map(([name,type])=>({name,type}));
const WRAPPER_FIELDS=[['contents','Order'],['name','string'],['version','string'],['chainId','uint256'],
 ['verifyingContract','address'],['salt','bytes32']].map(([name,type])=>({name,type}));
const lower=value=>String(value).toLowerCase();
function address(value){
 assert.equal(typeof value,'string');assert.match(value,/^0x[0-9a-fA-F]{40}$/);
 assert.notEqual(lower(value),'0x'+'00'.repeat(20));return lower(value);
}
function hexBytes(value){
 assert.equal(typeof value,'string');assert.match(value,/^0x(?:[0-9a-fA-F]{2})+$/);
 assert.ok(value.length<=8194,'Oversized signature envelope');
 return Buffer.from(value.slice(2),'hex');
}

export function validateWalletIdentity({intent,prepared,market}){
 const {order,typed}=prepared,type=intent.signatureType??0;
 assert.ok(Number.isInteger(type)&&type>=0&&type<=3,'Unsupported signature type');
 const signer=address(intent.signer??(type===0?intent.funder:undefined)),funder=address(intent.funder);
 const role=intent.signerRole??'owner';
 assert.ok(['owner','session'].includes(role),'Unknown signer role');
 assert.ok(role!=='session'||type===3,'Session signatures require a Deposit Wallet');
 assert.equal(order.signatureType,type,'Frozen signature type changed');
 assert.equal(address(order.maker),funder,'Wrong account wallet');
 assert.equal(address(order.signer),type===3?funder:signer,'Wrong order signer');
 if(type===0)assert.equal(signer,funder,'EOA signer and funder must match');
 else assert.notEqual(signer,funder,'Smart-wallet owner and account must be distinct');
 assert.deepEqual(typed.types.Order,ORDER_FIELDS,'Unexpected signed Order schema');
 assert.deepEqual(typed.types.EIP712Domain,DOMAIN_FIELDS,'Unexpected EIP712 domain schema');
 assert.equal(typed.domain.name,'Polymarket CTF Exchange');assert.equal(typed.domain.version,'2');
 assert.equal(Number(typed.domain.chainId),137);assert.equal(typeof market.negRisk,'boolean');
 const exchange=market.negRisk?'0xe2222d279d744050d28e00520010520000310f59':'0xe111180000d2663c0091e4f400237545b87b996b';
 assert.equal(address(typed.domain.verifyingContract),exchange,'Wrong exchange domain');
 assert.deepEqual(Object.keys(typed.domain).sort(),['chainId','name','verifyingContract','version']);
 let contents=typed.message,signature=order.signature;
 if(role==='session'){
  const envelope=hexBytes(signature);
  assert.equal(envelope.subarray(-32).toString('hex'),SESSION_MAGIC,'Missing session signature marker');
  const payload='0x'+envelope.subarray(0,-32).toString('hex'),coder=AbiCoder.defaultAbiCoder();
  const [signerId,reserved,inner]=coder.decode(SESSION_ABI,payload);
  assert.equal(lower(coder.encode(SESSION_ABI,[signerId,reserved,inner])),payload,
   'Noncanonical session ABI envelope');
  assert.equal(lower(signerId),'0x'+signer.slice(2).padStart(64,'0'),'Session signer ID mismatch');
  assert.equal(lower(reserved),ZERO,'Unsupported session envelope extension');
  signature=inner;
 }
 if(type===3){
  assert.equal(typed.primaryType,'TypedDataSign');
  assert.deepEqual(Object.keys(typed.types).sort(),['EIP712Domain','Order','TypedDataSign']);
  assert.deepEqual(typed.types.TypedDataSign,WRAPPER_FIELDS,'Unexpected deposit wrapper schema');
  const wrapper=typed.message;
  assert.deepEqual(Object.keys(wrapper).sort(),['chainId','contents','name','salt','verifyingContract','version']);
  assert.equal(wrapper.name,'DepositWallet');assert.equal(wrapper.version,'1');
  assert.equal(Number(wrapper.chainId),137);assert.equal(address(wrapper.verifyingContract),funder);
  assert.equal(lower(wrapper.salt),ZERO);contents=wrapper.contents;
  const bytes=hexBytes(signature),description=TypedDataEncoder.from({Order:ORDER_FIELDS}).encodeType('Order');
  const length=Buffer.byteLength(description,'utf8');
  assert.equal(bytes.length,65+32+32+length+2,'Invalid wrapped signature length');
  assert.equal(bytes.readUInt16BE(bytes.length-2),length,'Invalid wrapper description length');
  assert.equal('0x'+bytes.subarray(65,97).toString('hex'),lower(TypedDataEncoder.hashDomain(typed.domain)),
   'Wrapper application domain does not match');
  assert.equal('0x'+bytes.subarray(97,129).toString('hex'),
   lower(TypedDataEncoder.hashStruct('Order',{Order:ORDER_FIELDS},contents)),
   'Wrapper order contents do not match');
  assert.equal(bytes.subarray(129,-2).toString('utf8'),description,'Wrapper order description changed');
  signature='0x'+bytes.subarray(0,65).toString('hex');
 }else{
  assert.equal(typed.primaryType,'Order');
  assert.deepEqual(Object.keys(typed.types).sort(),['EIP712Domain','Order']);
  assert.equal(hexBytes(signature).length,65,'Unsupported ECDSA signature length');
 }
 assert.deepEqual(Object.keys(contents).sort(),ORDER_FIELDS.map(f=>f.name).sort());
 for(const {name} of ORDER_FIELDS){
  const expected=name==='side'?0:order[name];
  if(['maker','signer'].includes(name))assert.equal(address(contents[name]),address(expected));
  else assert.equal(String(contents[name]),String(expected),`Signed order mismatch: ${name}`);
 }
 const {EIP712Domain,...types}=typed.types;
 assert.equal(address(verifyTypedData(typed.domain,types,typed.message,signature)),signer,
  'Signature does not recover frozen signer');
 return {scope:role==='session'?'offline-session-signature-binding':'offline-owner-signature-binding',signature_type:type,
  session_envelope_verified:role==='session',delegated_permission_verified:false,
  recovered_signer_matches_intent:true,wrapped_signature_verified:type===3,
  onchain_owner_and_wallet_type_verified:false,onchain_eip1271_verified:false,
  account_and_fees_reconciled:false,can_submit:false,contents};
}
