/** Offline SDK compatibility research. No native network or real account. */
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync,writeFileSync,mkdirSync,openSync,fsyncSync,closeSync,renameSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {validateOfflineOrder} from './sdk_order_guard.mjs';

const root=new URL('../',import.meta.url);
const requireCrypto=createRequire(new URL('.polycast/oss-chainlink-sdk/package.json',root));
const {Wallet,TypedDataEncoder,verifyTypedData}=requireCrypto('ethers');
const sha256=value=>createHash('sha256').update(value).digest('hex');
const json=value=>JSON.stringify(value,(_,v)=>typeof v==='bigint'?v.toString():v);
function signingTypes(typed){const {EIP712Domain,...types}=typed.types;return types;}
function digest(typed){return TypedDataEncoder.hash(typed.domain,signingTypes(typed),typed.message);}
const fixtureDirectory=new URL('.polycast/sdk-order-offline-20261002/',root);
const manifestPath=new URL('frozen-order.json',fixtureDirectory);

function validateFrozen(frozen){
  assert.equal(frozen.scope,'synthetic_offline_only');
  assert.equal(sha256(frozen.wire_json),frozen.wire_sha256,'Wire body changed');
  assert.equal(digest(frozen.typed),frozen.typed_data_digest,'Signing payload changed');
  assert.equal(verifyTypedData(frozen.typed.domain,signingTypes(frozen.typed),
    frozen.typed.message,frozen.signed_order.signature),frozen.signed_order.signer);
  const wire=JSON.parse(frozen.wire_json);
  assert.equal(wire.orderType,'FOK');assert.equal(wire.owner,'SYNTHETIC-OFFLINE-OWNER');
  assert.equal(wire.deferExec,false);
  const expectedFields=['builder','expiration','maker','makerAmount','metadata','salt','side',
    'signature','signatureType','signer','takerAmount','timestamp','tokenId'];
  assert.deepEqual(Object.keys(wire.order).sort(),expectedFields.sort());
  for(const key of expectedFields){
    assert.equal(String(wire.order[key]),String(frozen.signed_order[key]),`Wire mismatch: ${key}`);
  }
  for(const {name} of signingTypes(frozen.typed).Order){
    const expected=name==='side'?(wire.order.side==='BUY'?0:1):wire.order[name];
    assert.equal(String(frozen.typed.message[name]),String(expected),`Signed mismatch: ${name}`);
  }
  assert.equal(frozen.signed_order.orderType,wire.orderType);
  assert.equal(frozen.signed_order.takerAmount,'5000000');
  assert.equal(frozen.signed_order.makerAmount,'2600000');
  assert.ok(Number.isSafeInteger(wire.order.salt)&&wire.order.salt>=0);
  return {wire_sha256:frozen.wire_sha256,typed_data_digest:frozen.typed_data_digest};
}

// Recovery child has no SDK import, signer/private key, auth factory or transport.
if(process.argv[2]==='--verify-frozen'){
  globalThis.fetch=()=>{throw Error('Network forbidden in offline recovery');};
  const frozen=JSON.parse(readFileSync(manifestPath,'utf8'));
  console.log(json({status:'passed',...validateFrozen(frozen)}));
  process.exit(0);
}

const wallet=Wallet.createRandom(); // Unfunded random key, never saved or printed.
const fixtureCondition='0x'+'11'.repeat(32);
const ctfToken=BigInt('0x'+'ab'.repeat(32)).toString();
const positionToken=BigInt('0x01'+'22'.repeat(18)+'00'.repeat(13)).toString();
let fixture={token:ctfToken,negRisk:false,foreignAsset:false};
const attempts=[],checks=[];
// Every SDK fetch terminates here. No native fetch is retained or called.
globalThis.fetch=async(input,init)=>{
  const request=new Request(input,init),url=new URL(request.url);
  attempts.push({method:request.method,path:url.pathname});
  assert.equal(request.method,'GET','Write request forbidden');
  assert.equal(url.origin,'https://clob.polymarket.com','Non-fixture origin forbidden');
  assert.ok(![...request.headers.keys()].some(k=>k.startsWith('poly_')||k==='authorization'),
    'Authentication header forbidden');
  let body;
  if(url.pathname==='/book')body={market:fixtureCondition,asset_id:fixture.token,
    timestamp:String(Date.now()),bids:[{price:'0.50',size:'100'}],asks:[{price:'0.52',size:'100'}],
    min_order_size:'5',tick_size:'0.01',neg_risk:fixture.negRisk,last_trade_price:'0.51',hash:'11'.repeat(20)};
  else if(url.pathname===`/clob-markets/${fixtureCondition}`)body={c:fixtureCondition,ao:true,
    v:'v1',mos:5,mts:.01,itode:true,nr:fixture.negRisk,fd:{r:.07,e:1,to:true},
    t:[{t:fixture.foreignAsset?'999':fixture.token,o:'Yes'},
      {t:(BigInt(fixture.token)+1n).toString(),o:'No'}]};
  else if(url.pathname===`/markets-by-token/${fixture.token}`)body={condition_id:fixtureCondition};
  else throw Error(`Fixture route unavailable: ${url.pathname}`);
  return new Response(json(body),{status:200,headers:{'content-type':'application/json'}});
};
async function check(name,fn){await fn();checks.push({name,passed:true});}
const report={started_at:new Date().toISOString(),scope:'offline_synthetic_order_format_only',
  sdk_version:'0.12.0',node_version:process.version,
  sdk_package_lock_sha256:sha256(readFileSync(new URL('.polycast/oss-polymarket-sdk/package-lock.json',root))),
  signature_recovery_library:'ethers '+requireCrypto('ethers').version,
  live_network_calls:0,real_credentials_used:false,user_wallet_used:false,
  orders_submitted:0,production_database_written:false,account_gate_passed:false,
  server_order_id_verified:false,onchain_signature_verified:false,checks,fixture_requests:attempts};
try{
  const sdkDir=new URL('.polycast/oss-polymarket-sdk/node_modules/@polymarket/client/',root);
  const {createPublicClient,SignerType,WalletType,OrderSide,OrderType}=await import(new URL('dist/index.js',sdkDir));
  const {prepareLimitOrder,prepareMarketOrder,postOrder}=await import(new URL('dist/actions/index.js',sdkDir));
  function clientFor({token=ctfToken,negRisk=false,foreignAsset=false,walletType=WalletType.EOA}={}){
    fixture={token,negRisk,foreignAsset};
    const client=createPublicClient();
    Object.defineProperty(client,'account',{value:{signer:wallet.address,
      wallet:walletType===WalletType.EOA?wallet.address:'0x'+'55'.repeat(20),
      signerType:SignerType.OWNER,walletType}});
    return client;
  }
  async function prepare(fn,request,client=clientFor()){
    const workflow=await fn(client,request),first=await workflow.next();
    assert.equal(first.done,false);assert.equal(first.value.kind,'signOrder');
    const typed=first.value.payload;
    const signature=await wallet.signTypedData(typed.domain,signingTypes(typed),typed.message);
    assert.equal(verifyTypedData(typed.domain,signingTypes(typed),typed.message,signature),wallet.address);
    const finished=await workflow.next(signature);
    assert.equal(finished.done,true);
    report.synthetic_signatures_generated=(report.synthetic_signatures_generated||0)+1;
    return {order:finished.value,typed,typed_data_digest:digest(typed)};
  }
  const limitRequest={assetId:ctfToken,side:OrderSide.BUY,price:'0.52',size:'5'};
  const marketRequest={assetId:ctfToken,side:OrderSide.BUY,amount:'5',maxPrice:'0.52'};
  const limit=await prepare(prepareLimitOrder,limitRequest);
  await check('limit_buy_five_shares_encode_as_six_decimal_units',()=>{
    assert.equal(limit.order.makerAmount,'2600000');assert.equal(limit.order.takerAmount,'5000000');
    assert.equal(limit.order.side,'BUY');assert.equal(limit.order.signatureType,0);
    assert.equal(limit.order.tokenId,ctfToken);assert.equal(limit.order.orderType,OrderType.GTC);
  });
  await check('eoas_signature_recovers_the_ephemeral_signer',()=>assert.equal(
    verifyTypedData(limit.typed.domain,signingTypes(limit.typed),limit.typed.message,limit.order.signature),wallet.address));
  await check('ctf_asset_uses_standard_exchange_domain_version_two',()=>{
    assert.equal(limit.typed.domain.chainId,137);assert.equal(limit.typed.domain.version,'2');
    assert.equal(limit.typed.domain.verifyingContract.toLowerCase(),'0xe111180000d2663c0091e4f400237545b87b996b');
  });
  report.limit_example={maker_amount:limit.order.makerAmount,taker_amount:limit.order.takerAmount,
    order_type:limit.order.orderType,signature_type:limit.order.signatureType,domain:limit.typed.domain};
  await check('negative_risk_ctf_asset_uses_negative_risk_exchange',async()=>{
    const value=await prepare(prepareLimitOrder,limitRequest,clientFor({negRisk:true}));
    assert.equal(value.typed.domain.version,'2');
    assert.equal(value.typed.domain.verifyingContract.toLowerCase(),'0xe2222d279d744050d28e00520010520000310f59');
    report.neg_risk_domain=value.typed.domain;
  });
  await check('protocol_v2_position_namespace_uses_domain_three_and_v3_exchange',async()=>{
    const value=await prepare(prepareLimitOrder,{...limitRequest,assetId:positionToken},clientFor({token:positionToken}));
    assert.equal(value.typed.domain.version,'3');
    assert.equal(value.typed.domain.verifyingContract.toLowerCase(),'0xe3333700ca9d93003f00f0f71f8515005f6c00aa');
    report.position_domain=value.typed.domain;
  });
  const market=await prepare(prepareMarketOrder,{...marketRequest,orderType:OrderType.FOK});
  await check('market_buy_amount_is_usd_and_explicit_fok_is_retained',()=>{
    assert.equal(market.order.makerAmount,'5000000');assert.equal(market.order.takerAmount,'9615400');
    assert.equal(market.order.orderType,OrderType.FOK);
  });
  report.market_example={maker_amount:market.order.makerAmount,taker_amount:market.order.takerAmount,
    order_type:market.order.orderType};
  await check('market_order_defaults_to_fak',async()=>assert.equal(
    (await prepare(prepareMarketOrder,marketRequest)).order.orderType,OrderType.FAK));
  await check('dollar_conversion_260_for_five_shares_at_052',async()=>{
    const value=await prepare(prepareMarketOrder,{...marketRequest,amount:'2.60',orderType:OrderType.FOK});
    assert.equal(value.order.makerAmount,'2600000');assert.equal(value.order.takerAmount,'5000000');
  });
  await check('all_in_max_spend_reduces_size_and_does_not_guarantee_market_minimum',async()=>{
    const value=await prepare(prepareMarketOrder,{...marketRequest,amount:'2.60',maxSpend:'2.60',orderType:OrderType.FOK});
    const dollars=Number(value.order.makerAmount)/1e6,shares=Number(value.order.takerAmount)/1e6;
    const worstPriceFee=shares*.07*.52*(1-.52);
    assert.ok(dollars+worstPriceFee<=2.60);assert.ok(shares<5);
    report.max_spend_example={maker_amount:value.order.makerAmount,taker_amount:value.order.takerAmount,
      conservative_estimated_cost:dollars+worstPriceFee,below_fixture_minimum:true};
  });
  await check('limit_sell_amounts_reverse_collateral_and_shares',async()=>{
    const value=await prepare(prepareLimitOrder,{...limitRequest,side:OrderSide.SELL});
    assert.equal(value.order.makerAmount,'5000000');assert.equal(value.order.takerAmount,'2600000');
  });
  await check('market_sell_input_is_shares',async()=>{
    const value=await prepare(prepareMarketOrder,{assetId:ctfToken,side:OrderSide.SELL,
      shares:'5',minPrice:'0.50',orderType:OrderType.FOK});
    assert.equal(value.order.makerAmount,'5000000');assert.equal(value.order.takerAmount,'2500000');
  });
  await check('limit_size_rounds_down_to_two_decimal_places',async()=>{
    const value=await prepare(prepareLimitOrder,{...limitRequest,size:'5.009'});
    assert.equal(value.order.takerAmount,'5000000');
  });
  await check('sdk_limit_preparation_does_not_enforce_five_share_minimum',async()=>{
    const value=await prepare(prepareLimitOrder,{...limitRequest,size:'4.99'});
    assert.equal(value.order.takerAmount,'4990000');report.adapter_minimum_guard_required=true;
  });
  await check('limit_off_grid_price_is_rejected',()=>assert.rejects(
    prepare(prepareLimitOrder,{...limitRequest,price:'0.525'}),/multiple of tick size/));
  await check('market_off_grid_protected_price_is_rejected',()=>assert.rejects(
    prepare(prepareMarketOrder,{...marketRequest,maxPrice:'0.525'}),/multiple of tick size/));
  await check('market_gtc_type_is_rejected',()=>assert.rejects(
    prepare(prepareMarketOrder,{...marketRequest,orderType:OrderType.GTC})));
  await check('short_gtd_expiration_is_rejected',()=>assert.rejects(
    prepare(prepareLimitOrder,{...limitRequest,expiration:Math.floor(Date.now()/1000)+60}),/3 minutes/));
  await check('valid_gtd_expiration_is_retained',async()=>{
    const expiration=Math.floor(Date.now()/1000)+240;
    const value=await prepare(prepareLimitOrder,{...limitRequest,expiration});
    assert.equal(value.order.orderType,OrderType.GTD);assert.equal(value.order.expiration,expiration);
  });
  await check('market_metadata_asset_mismatch_is_rejected',()=>assert.rejects(
    prepare(prepareLimitOrder,limitRequest,clientFor({foreignAsset:true})),/does not include asset/));
  await check('proxy_and_safe_maker_and_signer_are_distinct',async()=>{
    for(const [walletType,signatureType] of [[WalletType.POLY_PROXY,1],[WalletType.GNOSIS_SAFE,2]]){
      const value=await prepare(prepareLimitOrder,limitRequest,clientFor({walletType}));
      assert.equal(value.order.maker,'0x'+'55'.repeat(20));assert.equal(value.order.signer,wallet.address);
      assert.equal(value.order.signatureType,signatureType);
    }
  });
  await check('deposit_wallet_uses_nested_typed_data_and_wrapped_signature',async()=>{
    const value=await prepare(prepareLimitOrder,limitRequest,clientFor({walletType:WalletType.DEPOSIT_WALLET}));
    assert.equal(value.order.signatureType,3);assert.equal(value.typed.primaryType,'TypedDataSign');
    assert.equal(value.order.signer,value.order.maker);assert.equal(value.typed.message.name,'DepositWallet');
    assert.ok(value.order.signature.length>132);
    report.deposit_wallet_example={primary_type:value.typed.primaryType,
      signature_type:value.order.signatureType,wrapped_signature_bytes:(value.order.signature.length-2)/2,
      contract_signature_validated:false};
  });

// The SDK's wire serializer runs with a throwing in-memory transport. There is
// no authentication or HTTP operation, including for the synthetic signed order.
  const captureStop=new Error('OFFLINE_CAPTURE_STOP');
  async function captureWire(order){
    let captured;
    const stub={credentials:{key:'SYNTHETIC-OFFLINE-OWNER'},secureClob:{post(path,options){
      assert.equal(path,'/order');captured=options.json;throw captureStop;
    }}};
    await assert.rejects(postOrder(stub)(order),error=>error===captureStop);
    assert.ok(captured);return captured;
  }
  const fokLimit={...limit.order,orderType:OrderType.FOK};
  const wire=await captureWire(fokLimit);
  const guardNow=Date.now();
  const guardFixture={nowMs:guardNow,intent:{protocol:'ctf-v1',side:'BUY',orderType:'FOK',
    funder:wallet.address,conditionId:fixtureCondition,tokenId:ctfToken,sharesMicro:'5000000',
    priceMicro:'520000',maxCostMicro:'2700000'},market:{protocol:'ctf-v1',conditionId:fixtureCondition,
    acceptingOrders:true,tokens:[ctfToken],negRisk:false,observedAtMs:guardNow,
    deadlineMs:guardNow+60000,executionDelayMs:250,minimumSharesMicro:'5000000',tickMicro:'10000',
    feeRateMicro:'70000',feeExponent:1,feeTakerOnly:true},prepared:{order:fokLimit,typed:limit.typed}};
  // The initial order may be several seconds old after the preceding fixtures;
  // use its real preparation timestamp, never alter/re-sign it to pass a guard.
  guardFixture.nowMs=Number(fokLimit.timestamp)+100;
  guardFixture.market.observedAtMs=guardFixture.nowMs;
  guardFixture.market.deadlineMs=guardFixture.nowMs+60000;
  await check('offline_guard_preserves_fixed_size_price_and_all_in_reserve',()=>{
    report.offline_guard=validateOfflineOrder(guardFixture);
    assert.equal(report.offline_guard.maximum_cost_micro,'2687360');
    assert.equal(report.offline_guard.can_submit,false);
  });
  const guardCase=(name,mutate,pattern)=>check(name,()=>{
    const bad=JSON.parse(json(guardFixture));mutate(bad);
    assert.throws(()=>validateOfflineOrder(bad),pattern);
  });
  await guardCase('offline_guard_rejects_dollars_as_fixed_share_quantity',
    x=>{x.prepared.order.takerAmount='9615400';},/quantity differs/);
  await guardCase('offline_guard_rejects_below_minimum_after_rounding',
    x=>{x.intent.sharesMicro='4990000';},/minimum size/);
  await guardCase('offline_guard_rejects_insufficient_fee_reserve',
    x=>{x.intent.maxCostMicro='2600000';},/cost exceeds/);
  await guardCase('offline_guard_rejects_wrong_signing_contract',
    x=>{x.prepared.typed.domain.verifyingContract='0x'+'66'.repeat(20);},/signing exchange/);
  await guardCase('offline_guard_rejects_expired_event',
    x=>{x.market.deadlineMs=x.nowMs;},/before event deadline/);
  await guardCase('offline_guard_rejects_stale_market_context',
    x=>{x.market.observedAtMs=x.nowMs-5001;},/Stale/);
  await guardCase('offline_guard_rejects_closed_market',
    x=>{x.market.acceptingOrders=false;});
  await guardCase('offline_guard_rejects_foreign_token',
    x=>{x.market.tokens=[];},/intended asset/);
  await guardCase('offline_guard_rejects_changed_fee_schedule',
    x=>{x.market.feeExponent=2;},/fee exponent/);
  await guardCase('offline_guard_rejects_price_increase',
    x=>{x.prepared.order.makerAmount='2700000';},/collateral differs/);
  await guardCase('offline_guard_rejects_quantity_that_sdk_would_round',
    x=>{x.intent.sharesMicro='5009000';},/precision would change/);
  await guardCase('offline_guard_rejects_protocol_namespace_conflict',
    x=>{x.intent.tokenId='101';x.prepared.order.tokenId='101';x.market.tokens=['101'];},/namespace conflicts/);
  await check('sdk_wire_serializer_retains_explicit_fok_and_five_shares',()=>{
    assert.equal(wire.orderType,'FOK');assert.equal(wire.order.takerAmount,'5000000');
    assert.equal(wire.order.makerAmount,'2600000');assert.equal(wire.deferExec,false);
    assert.ok(Number.isSafeInteger(wire.order.salt));assert.equal(String(wire.order.salt),limit.order.salt);
    assert.ok(!('orderType' in limit.typed.message));
  });
  await check('sdk_rejects_post_only_with_fok_before_offline_transport',()=>assert.rejects(
    captureWire({...fokLimit,postOnly:true}),/Post-only orders/));
  await check('sdk_rejects_unsafe_salt_before_offline_transport',()=>assert.rejects(
    captureWire({...fokLimit,salt:'9007199254740992'}),/safe integer/));
  const frozen={scope:'synthetic_offline_only',created_at:new Date().toISOString(),
    typed:limit.typed,typed_data_digest:limit.typed_data_digest,signed_order:fokLimit,
    wire_json:json(wire),wire_sha256:sha256(json(wire)),server_order_id_verified:false};
  mkdirSync(fixtureDirectory,{recursive:true});
  const temp=new URL('frozen-order.json.tmp',fixtureDirectory),fd=openSync(temp,'w');
  try{writeFileSync(fd,json(frozen));fsyncSync(fd);}finally{closeSync(fd);}
  renameSync(temp,manifestPath);
  await check('frozen_sdk_signature_and_exact_wire_body_survive_a_new_process',()=>{
    const childEnv=Object.fromEntries(Object.entries(process.env).filter(([k])=>
      ['SYSTEMROOT','WINDIR','TEMP','TMP','PATH','PATHEXT'].includes(k.toUpperCase())));
    const child=spawnSync(process.execPath,[fileURLToPath(import.meta.url),'--verify-frozen'],
      {encoding:'utf8',windowsHide:true,env:childEnv,timeout:15000});
    assert.equal(child.status,0,child.stderr);
    const result=JSON.parse(child.stdout);
    assert.equal(result.wire_sha256,frozen.wire_sha256);assert.equal(result.typed_data_digest,frozen.typed_data_digest);
    report.recovery={...result,independent_process:true,private_key_available:false,
      automatic_resubmission_allowed:false};
  });
  await check('wire_mutation_is_detected_by_the_frozen_body_hash',()=>{
    const bad=JSON.parse(json(frozen));bad.wire_json=bad.wire_json.replace('5000000','5000001');
    assert.throws(()=>validateFrozen(bad),/Wire body changed/);
  });
  await check('changed_signature_domain_is_detected',()=>{
    const bad=JSON.parse(json(frozen));bad.typed.domain.chainId=1;
    assert.throws(()=>validateFrozen(bad),/Signing payload changed/);
  });
  await check('recreating_the_same_intent_generates_a_different_order_digest',async()=>{
    const recreated=await prepare(prepareLimitOrder,limitRequest);
    assert.notEqual(recreated.order.salt,limit.order.salt);assert.notEqual(recreated.typed_data_digest,limit.typed_data_digest);
    report.recreate_is_not_safe_retry=true;
  });
  report.source_evidence=[];
  for(const file of ['chunk-5JHFOBBZ.js.map','index.js.map']){
    const map=JSON.parse(readFileSync(new URL('dist/'+file,sdkDir),'utf8'));
    for(let i=0;i<map.sources.length;i++){
      const name=map.sources[i],source=map.sourcesContent[i];
      if(source&&['../src/protocol.ts','../src/exchange.ts','../src/actions/orders/context.ts',
        '../src/actions/orders/orders.ts','../src/actions/orders/amounts.ts',
        '../src/actions/orders/post.ts'].includes(name)){
        report.source_evidence.push({source:name,sha256:sha256(source)});
      }
    }
  }
  report.status='passed';
}catch(error){
  report.status='failed';report.error={name:error.name,message:String(error.message).slice(0,400)};
  process.exitCode=1;
}finally{
  report.completed_at=new Date().toISOString();report.source_sha256=sha256(readFileSync(new URL(import.meta.url)));
  report.guard_source_sha256=sha256(readFileSync(new URL('sdk_order_guard.mjs',import.meta.url)));
  writeFileSync(new URL('.polycast/polymarket-order-validation-20261002.json',root),json(report));
  console.log(json({status:report.status,checks:checks.length,fixture_requests:attempts.length,
    limit:report.limit_example,market:report.market_example,max_spend:report.max_spend_example,
    recovery:report.recovery,error:report.error}));
}
