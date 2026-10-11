/** Official secure client in a bounded in-memory exchange; no native networking. */
import assert from 'node:assert/strict';
import {readFileSync,writeFileSync} from 'node:fs';
import {createHash,createHmac,randomBytes} from 'node:crypto';

const root=new URL('../',import.meta.url);
const sdk=new URL('.polycast/oss-polymarket-sdk/node_modules/@polymarket/client/',root);
const frozen=JSON.parse(readFileSync(new URL('.polycast/sdk-order-offline-20261002/frozen-order.json',root)));
const sdkVersion=JSON.parse(readFileSync(new URL('package.json',sdk))).version;
assert.equal(sdkVersion,'0.12.0');
const bodyFrozen=JSON.parse(frozen.wire_json);
const address=frozen.signed_order.signer;
const credentials={key:bodyFrozen.owner,secret:randomBytes(32).toString('base64'),passphrase:'OFFLINE-FIXTURE'};
const condition='0x'+'44'.repeat(32), token=frozen.signed_order.tokenId;
const checks=[],trace=[],captured=[];
const sha=value=>createHash('sha256').update(value).digest('hex');
function check(name,fn){fn();checks.push({name,passed:true});}
function signature(timestamp,method,path,body=''){
 return createHmac('sha256',Buffer.from(credentials.secret,'base64'))
   .update(timestamp+method+path+body).digest('base64').replaceAll('+','-').replaceAll('/','_');
}
const orderRaw={id:frozen.typed_data_digest,market:condition,asset_id:token,owner:credentials.key,
 maker_address:address,side:'BUY',price:'0.52',original_size:'5',size_matched:'5',outcome:'Yes',
 order_type:'FOK',status:'MATCHED',associate_trades:['fixture-trade'],created_at:1791012000,expiration:'0'};
const tradeRaw={id:'fixture-trade',market:condition,asset_id:token,owner:credentials.key,
 maker_address:address,taker_order_id:frozen.typed_data_digest,side:'BUY',trader_side:'TAKER',
 price:'0.52',size:'5',outcome:'Yes',status:'CONFIRMED',fee_rate_bps:'700',bucket_index:0,
 transaction_hash:'0x'+'55'.repeat(32),maker_orders:[],match_time:'1791012001',last_update:'1791012002'};
let failureMode=null;
globalThis.fetch=async(input,init)=>{
 const request=new Request(input,init),url=new URL(request.url);
 assert.equal(url.origin,'https://clob.polymarket.com');
 assert.ok(trace.length<20,'Fixture request budget exceeded');
 const body=await request.text();
 for(const name of ['POLY_ADDRESS','POLY_API_KEY','POLY_PASSPHRASE','POLY_TIMESTAMP','POLY_SIGNATURE'])
   assert.ok(request.headers.has(name),`Missing L2 header ${name}`);
 assert.equal(request.headers.get('POLY_ADDRESS').toLowerCase(),address.toLowerCase());
 assert.equal(request.headers.get('POLY_API_KEY'),credentials.key);
 assert.equal(request.headers.get('POLY_PASSPHRASE'),credentials.passphrase);
 const timestamp=request.headers.get('POLY_TIMESTAMP');
 assert.match(timestamp,/^\d+$/);
 assert.ok(Math.abs(Date.now()/1000-Number(timestamp))<5);
 const actual=request.headers.get('POLY_SIGNATURE');
 assert.equal(actual,signature(timestamp,request.method,url.pathname,body));
 captured.push({timestamp,method:request.method,path:url.pathname,body,actual});
 trace.push({method:request.method,path:url.pathname,query:Object.fromEntries(url.searchParams),
             body_sha256:sha(body),hmac_independently_validated:true});
 const response=(json,status=200)=>new Response(JSON.stringify(json),{status,headers:{'content-type':'application/json'}});
 if(request.method==='POST'&&url.pathname==='/order'){
   assert.equal(body,frozen.wire_json,'Exact frozen SDK serialization changed');
   if(failureMode==='timeout')throw new TypeError('Synthetic ambiguous transport failure');
   if(failureMode==='http500')return response({error:'Synthetic server failure'},500);
   return response({errorMsg:'',orderID:frozen.typed_data_digest,takingAmount:'5',makingAmount:'2.6',
                    status:'matched',success:true,transactionsHashes:[],tradeIDs:['fixture-trade']});
 }
 assert.equal(request.method,'GET');
 if(url.pathname==='/auth/api-keys')return response({apiKeys:[credentials.key]});
 if(url.pathname===`/data/order/${frozen.typed_data_digest}`)return response(orderRaw);
 if(url.pathname==='/data/orders')return response({limit:100,next_cursor:'LTE=',count:0,data:[]});
 if(url.pathname==='/data/trades'){
   const second=url.searchParams.get('next_cursor')==='Mg==';
   return response({limit:100,next_cursor:second?'LTE=':'Mg==',count:second?0:1,data:second?[]:[tradeRaw]});
 }
 throw new Error('Endpoint outside fixture allowlist');
};
let walletOperations=0;
const noWallet=()=>{walletOperations++;throw new Error('Wallet operation forbidden in fixture');};
const started=new Date().toISOString();
const report={started_at:started,scope:'synthetic_secure_sdk_transport_and_normalization_only',sdk_version:sdkVersion,
 native_network_calls:0,real_credentials_used:false,user_wallet_used:false,real_orders_submitted:0,
 production_database_written:false,real_server_order_id_verified:false,account_gate_passed:false};
try{
 const {createSecureClient}=await import(new URL('dist/index.js',sdk));
 const client=await createSecureClient({wallet:address,credentials,
   signer:{getAddress:async()=>address,signTypedData:noWallet,signMessage:noWallet,sendTransaction:noWallet}});
 check('provided_fixture_credentials_do_not_invoke_wallet_operations',()=>assert.equal(walletOperations,0));
 const ack=await client.postOrder(frozen.signed_order);
 report.ack=ack;
 check('sdk_post_response_retains_expected_fixture_order_identity',()=>assert.equal(ack.orderID??ack.orderId,frozen.typed_data_digest));
 const order=await client.fetchOrder({orderId:frozen.typed_data_digest});
 const openPages=[];for await(const page of client.listOpenOrders())openPages.push(page);
 const tradePages=[];for await(const page of client.listAccountTrades())tradePages.push(page);
 report.sdk_normalized={order,open_order_pages:openPages,account_trade_pages:tradePages};
 check('order_normalized_token_id_and_compatibility_alias_agree',()=>{assert.equal(order.tokenId,token);assert.equal(order.assetId,token);});
 check('sdk_account_trade_pages_are_consumed_through_terminal_cursor',()=>{
   assert.equal(tradePages.length,2);assert.equal(tradePages.flatMap(p=>p.items).length,1);
 });
 const trade=tradePages[0].items[0];
 check('account_trade_has_rate_but_no_measured_fee_amount',()=>{
   assert.equal(trade.feeRateBps,'700');assert.equal(trade.feeUsdc,undefined);assert.equal(trade.fee_paid_micro,undefined);
 });
 check('hmac_authentication_binds_exact_serialized_body',()=>{
   const record=captured.find(r=>r.method==='POST');
   for(const bad of [record.body+' ',JSON.stringify({...JSON.parse(record.body),orderType:'GTC'})])
     assert.notEqual(signature(record.timestamp,record.method,record.path,bad),record.actual);
 });
 check('hmac_authentication_binds_timestamp_method_and_route',()=>{
   const r=captured.find(r=>r.method==='POST');
   for(const [t,m,p] of [[String(Number(r.timestamp)+1),r.method,r.path],[r.timestamp,'GET',r.path],
                         [r.timestamp,r.method,'/orders']])assert.notEqual(signature(t,m,p,r.body),r.actual);
 });
 check('get_query_is_not_appended_to_the_sdk_signed_route',()=>{
   const r=captured.find(r=>r.path==='/data/trades');assert.ok(r);
   assert.equal(r.actual,signature(r.timestamp,'GET','/data/trades'));
 });
 for(const fault of ['timeout','http500']){
   failureMode=fault;const before=trace.length;
   await assert.rejects(client.postOrder(frozen.signed_order));
   check(`sdk_${fault}_post_is_not_automatically_retried`,()=>assert.equal(trace.length-before,1));
 }
 report.sdk_normalized={order,open_order_pages:openPages,account_trade_pages:tradePages};
 report.frozen_identity={expected_order_id:frozen.typed_data_digest,condition_id:condition,token_id:token,
   funder:address,shares:'5',limit_price:'0.52',order_type:'FOK',reserve_micro:2687360,maximum_fee_micro:87360};
 report.fixture_account={scope:'synthetic_dedicated_account_observation',capital_micro:50000000,
   cash_micro:47312650,positions:{[token]:'5'},fee_receipts:{'fixture-trade':{
     scope:'synthetic_measured_fee_receipt',trade_id:'fixture-trade',order_id:frozen.typed_data_digest,
     condition_id:condition,token_id:token,funder:address,transaction_hash:tradeRaw.transaction_hash,
     status:'CONFIRMED',fee_paid_micro:87350}}};
 report.frozen_order_sha256=sha(readFileSync(new URL('.polycast/sdk-order-offline-20261002/frozen-order.json',root)));
 report.completed_at=new Date().toISOString();report.checks=checks;report.requests=trace;
 report.passed=checks.length;report.wallet_operations=walletOperations;report.auth_header_values_saved=false;
 report.source_sha256=sha(readFileSync(new URL(import.meta.url)));
 writeFileSync(new URL('.polycast/sdk-authenticated-fixture-20261003.json',root),JSON.stringify(report,null,2)+'\n');
 console.log(JSON.stringify({passed:checks.length,fixture_requests:trace.length,native_network_calls:0,
   normalized_order_keys:Object.keys(order),normalized_trade_keys:Object.keys(trade)}));
}catch(error){
 writeFileSync(new URL('.polycast/sdk-authenticated-fixture-failed-20261003.json',root),JSON.stringify({
   ...report,status:'failed',fixture_paths:trace.map(({method,path})=>({method,path})),checks_passed:checks.length,
   failure_type:error?.constructor?.name,source_sha256:sha(readFileSync(new URL(import.meta.url)))},null,2)+'\n');
 console.log(JSON.stringify({status:'failed',error_type:error?.constructor?.name,message:String(error.message).slice(0,300),
   fixture_paths:trace.map(({method,path})=>({method,path})),checks_passed:checks.length,fixture_requests:trace.length,native_network_calls:0}));process.exitCode=1;
}
