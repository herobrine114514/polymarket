/** Offline SDK order-format research. Synthetic account/market, no real credentials. */
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync,writeFileSync} from 'node:fs';
import {createHash,randomBytes} from 'node:crypto';

const root=new URL('../',import.meta.url);
const requireCrypto=createRequire(new URL('.polycast/oss-chainlink-sdk/package.json',root));
const {Wallet,TypedDataEncoder,verifyTypedData}=requireCrypto('ethers');
const wallet=Wallet.createRandom(); // Ephemeral, unfunded, never saved or printed.
const fixtureCondition='0x'+'11'.repeat(32);
const fixtureToken='101';
const attempts=[],checks=[];
let negRisk=false;
const fixtureBook={market:fixtureCondition,asset_id:fixtureToken,timestamp:String(Date.now()),
  bids:[{price:'0.50',size:'100'}],asks:[{price:'0.52',size:'100'}],
  min_order_size:'5',tick_size:'0.01',neg_risk:false,last_trade_price:'0.51',hash:'11'.repeat(20)};
const fixtureInfo={c:fixtureCondition,ao:true,v:'v1',mos:5,mts:.01,itode:true,
  fd:{r:.07,e:1,to:true},t:[{t:fixtureToken,o:'Yes'},{t:'102',o:'No'}]};
// No fallback to native fetch. Even a wrong route cannot reach any network.
globalThis.fetch=async(input,init)=>{
  const request=new Request(input,init),url=new URL(request.url);
  attempts.push({method:request.method,path:url.pathname,query:Object.fromEntries(url.searchParams)});
  assert.equal(request.method,'GET','Write request forbidden');
  assert.equal(url.origin,'https://clob.polymarket.com','Non-fixture origin forbidden');
  let body;
  if(url.pathname==='/book')body={...fixtureBook,neg_risk:negRisk,timestamp:String(Date.now())};
  else if(url.pathname===`/clob-markets/${fixtureCondition}`)body={...fixtureInfo,nr:negRisk};
  else if(url.pathname==='/token/condition')body={condition_id:fixtureCondition};
  else if(url.pathname===`/markets-by-token/${fixtureToken}`)body={condition_id:fixtureCondition};
  else if(url.pathname==='/neg-risk')body={neg_risk:negRisk};
  else if(url.pathname==='/tick-size')body={minimum_tick_size:.01};
  else if(url.pathname==='/time')body=Math.floor(Date.now()/1000);
  else throw Error(`Fixture route unavailable: ${url.pathname}`);
  return new Response(JSON.stringify(body),{status:200,headers:{'content-type':'application/json'}});
};
function check(name,fn){fn();checks.push({name,passed:true});}
function signingTypes(typed){
  // ethers computes the domain type itself; the SDK supplies it explicitly.
  const {EIP712Domain,...types}=typed.types;
  return types;
}
const report={started_at:new Date().toISOString(),scope:'offline_synthetic_order_format_only',
  sdk_version:'0.12.0',live_network_calls:0,real_credentials_used:false,user_wallet_used:false,
  orders_submitted:0,production_database_written:false,account_gate_passed:false,
  server_order_id_verified:false,checks,fixture_requests:attempts};
try{
  const sdkDir=new URL('.polycast/oss-polymarket-sdk/node_modules/@polymarket/client/',root);
  const {createPublicClient,SignerType,WalletType,OrderSide,OrderType}=await import(new URL('dist/index.js',sdkDir));
  const {prepareLimitOrder,prepareMarketOrder}=await import(new URL('dist/actions/index.js',sdkDir));
  const client=createPublicClient();
  // The pure preparation workflow needs account context. This synthetic context
  // is injected on a PUBLIC client, never through a real authentication factory.
  Object.defineProperty(client,'account',{value:{signer:wallet.address,wallet:wallet.address,
    signerType:SignerType.OWNER,walletType:WalletType.EOA}});
  async function prepare(fn,request){
    const workflow=await fn(client,request);
    const first=await workflow.next();
    assert.equal(first.done,false);assert.equal(first.value.kind,'signOrder');
    const typed=first.value.payload;
    const signature=await wallet.signTypedData(typed.domain,signingTypes(typed),typed.message);
    assert.equal(verifyTypedData(typed.domain,signingTypes(typed),typed.message,signature),wallet.address);
    const finished=await workflow.next(signature);
    assert.equal(finished.done,true);
    return {order:finished.value,typed,
      typed_data_digest:TypedDataEncoder.hash(typed.domain,signingTypes(typed),typed.message)};
  }
  const limit=await prepare(prepareLimitOrder,{assetId:fixtureToken,side:OrderSide.BUY,price:'0.52',size:'5'});
  check('limit_buy_shares_encode_as_six_decimal_units',()=>{
    assert.equal(limit.order.makerAmount,'2600000');assert.equal(limit.order.takerAmount,'5000000');
    assert.equal(limit.order.side,'BUY');assert.equal(limit.order.signatureType,0);
    assert.equal(limit.order.tokenId,fixtureToken);assert.equal(limit.order.orderType,OrderType.GTC);
  });
  check('eoas_signature_matches_the_ephemeral_signer',()=>assert.equal(
    verifyTypedData(limit.typed.domain,signingTypes(limit.typed),limit.typed.message,limit.order.signature),wallet.address));
  report.limit_example={maker_amount:limit.order.makerAmount,taker_amount:limit.order.takerAmount,
    order_type:limit.order.orderType,signature_type:limit.order.signatureType,
    domain:limit.typed.domain,typed_data_digest:limit.typed_data_digest};
  const market=await prepare(prepareMarketOrder,{assetId:fixtureToken,side:OrderSide.BUY,amount:'5',maxPrice:'0.52',orderType:OrderType.FOK});
  check('market_buy_amount_is_usd_and_explicit_fok_is_retained',()=>{
    assert.equal(market.order.makerAmount,'5000000');
    assert.notEqual(market.order.takerAmount,'5000000');
    assert.equal(market.order.orderType,OrderType.FOK);
  });
  report.market_example={maker_amount:market.order.makerAmount,taker_amount:market.order.takerAmount,
    order_type:market.order.orderType,typed_data_digest:market.typed_data_digest};
  report.status='passed';
}catch(error){
  report.status='failed';report.error={name:error.name,message:String(error.message).slice(0,250)};
  process.exitCode=1;
}finally{
  report.completed_at=new Date().toISOString();
  report.source_sha256=createHash('sha256').update(readFileSync(new URL(import.meta.url))).digest('hex');
  writeFileSync(new URL('.polycast/polymarket-order-probe-20261002.json',root),JSON.stringify(report,null,2));
  console.log(JSON.stringify({status:report.status,checks:checks.length,fixture_requests:attempts,
    limit:report.limit_example,market:report.market_example,error:report.error}));
}
