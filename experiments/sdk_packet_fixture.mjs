/** Native SDK -> atomic replay packet fixtures; no native network or user keys. */
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {freezeSyntheticOrderPacket,verifySyntheticOrderPacket} from './sdk_order_packet.mjs';
const root=new URL('../',import.meta.url),sdk=new URL('.polycast/oss-polymarket-sdk/node_modules/@polymarket/client/',root);
const requireCrypto=createRequire(new URL('.polycast/oss-chainlink-sdk/package.json',root));
const {Wallet}=requireCrypto('ethers'),owner=Wallet.createRandom(),delegate=Wallet.createRandom();
const wallet='0x'+'55'.repeat(20),condition='0x'+'11'.repeat(32),token=BigInt('0x'+'ab'.repeat(32)).toString();
let risk=false,reads=0;const packets=[];
globalThis.fetch=async(input,init)=>{
 const req=new Request(input,init),url=new URL(req.url);reads++;
 assert.equal(req.method,'GET');assert.equal(url.origin,'https://clob.polymarket.com');assert.ok(reads<=25);
 let raw;
 if(url.pathname==='/book')raw={market:condition,asset_id:token,timestamp:String(Date.now()),bids:[{price:'.50',size:'100'}],
  asks:[{price:'.52',size:'100'}],min_order_size:'5',tick_size:'.01',neg_risk:risk,last_trade_price:'.51',hash:'11'.repeat(20)};
 else if(url.pathname===`/clob-markets/${condition}`)raw={c:condition,ao:true,v:'v1',mos:5,mts:.01,itode:true,nr:risk,
  fd:{r:.07,e:1,to:true},t:[{t:token,o:'Yes'},{t:(BigInt(token)+1n).toString(),o:'No'}]};
 else if(url.pathname===`/markets-by-token/${token}`)raw={condition_id:condition};
 else throw Error('Forbidden synthetic route');
 return new Response(JSON.stringify(raw),{headers:{'content-type':'application/json'}});
};
const {createPublicClient,SignerType}=await import(new URL('dist/index.js',sdk));
const {prepareLimitOrder}=await import(new URL('dist/actions/index.js',sdk));
for(const nr of [false,true])for(const [type,role] of [[0,'owner'],[1,'owner'],[2,'owner'],[3,'owner'],[3,'session']]){
 risk=nr;const signer=role==='session'?delegate:owner,funder=type===0?signer.address:wallet,client=createPublicClient();
 Object.defineProperty(client,'account',{value:{signer:signer.address,wallet:funder,walletType:type,
  signerType:role==='session'?SignerType.SESSION_KEY:SignerType.OWNER}});
 const flow=await prepareLimitOrder(client,{assetId:token,side:'BUY',price:'0.52',size:'5'}),first=await flow.next();
 assert.equal(first.value.kind,'signOrder');const typed=first.value.payload,{EIP712Domain,...types}=typed.types;
 const finish=await flow.next(await signer.signTypedData(typed.domain,types,typed.message));assert.equal(finish.done,true);
 const nowMs=Number(finish.value.timestamp)+100;
 const context={nowMs,intent:{protocol:'ctf-v1',side:'BUY',orderType:'FOK',signatureType:type,signerRole:role,
  signer:signer.address,funder,conditionId:condition,tokenId:token,sharesMicro:'5000000',priceMicro:'520000',maxCostMicro:'2687360'},
  market:{protocol:'ctf-v1',conditionId:condition,acceptingOrders:true,tokens:[token],negRisk:nr,observedAtMs:nowMs,
   deadlineMs:nowMs+60000,executionDelayMs:250,minimumSharesMicro:'5000000',tickMicro:'10000',feeRateMicro:'70000',
   feeExponent:1,feeTakerOnly:true},prepared:{typed,order:{...finish.value,orderType:'FOK'}}};
 if(role==='session'){
  context.sessionRegistry={scope:'unverified-offline-owner-registry-snapshot',path:'/v1/user/session-signers',
   account:{signer:owner.address,funder,wallet_type:3,credential_scope:'owner'},startedAtMs:nowMs-200,
   receivedAtMs:nowMs-100,response:{wallet:funder,signers:[{address:signer.address,scopes:['CLOB'],valid_until:Math.floor(nowMs/1000)+15534000}]}};
  context.localLease={signer:signer.address,wallet:funder,createdAtMs:nowMs-1000,expiresAtMs:nowMs+180000,paused:false};
 }
 const packet=await freezeSyntheticOrderPacket({context,intentId:`sdk-${type}-${role}-${nr}`,riskGroup:`window-${nr}`});
 const verified=await verifySyntheticOrderPacket(packet,{checkedAtMs:nowMs+10});assert.equal(verified.can_submit,false);
 assert.equal(packet.hashes.order_typehash,'0xbb86318a2138f5fa8ae32fbe8e659f8fcf13cc6ae4014a707893055433818589');
 if(type===3)assert.notEqual(packet.hashes.contract_order_hash,packet.hashes.signing_digest);
 else assert.equal(packet.hashes.contract_order_hash,packet.hashes.signing_digest);
 packets.push(packet);
}
console.log(JSON.stringify(process.argv.includes('--emit')?{scope:'native-sdk-synthetic-packets',packets}:
 {scope:'native-sdk-synthetic-packets',packets:packets.length,synthetic_public_reads:reads,native_network_calls:0,real_orders_submitted:0}));
