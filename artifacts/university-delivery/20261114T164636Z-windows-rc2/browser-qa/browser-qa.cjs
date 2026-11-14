const fs = require('fs');
const path = require('path');
const os = require('os');
const crypto = require('crypto');
const args = Object.fromEntries(process.argv.slice(2).map(arg => {
  const i = arg.indexOf('='); return i < 0 ? [arg.replace(/^--/, ''), true] : [arg.slice(0, i).replace(/^--/, ''), arg.slice(i+1)];
}));
const toolDir = args['tool-dir'] || path.join(os.tmpdir(), 'solverscout-windows-rc2-browser-20261114T164636Z');
const { chromium } = require(path.join(toolDir, 'node_modules', 'playwright-core'));
const packageInfo = require(path.join(toolDir, 'node_modules', 'playwright-core', 'package.json'));
const out = path.resolve(args.out || 'artifacts/university-delivery/20261114T164636Z-windows-rc2/browser-qa');
const browserPath = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const url = args.url || 'http://127.0.0.1:8501';
const mode = args.mode || 'setup';
const write = (name, value) => fs.writeFileSync(path.join(out, name), JSON.stringify(value, null, 2) + '\n', 'utf8');
const sha = file => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
fs.mkdirSync(out, {recursive:true});
(async () => {
  const browser = await chromium.launch({executablePath:browserPath, headless:true});
  try {
    const environment = {
      schema_version:'windows-browser-qa/1.0', captured_at_utc:new Date().toISOString(),
      browser_executable:browserPath, browser_version:browser.version(), playwright_core_version:packageInfo.version,
      node_version:process.version, node_executable:process.execPath, tooling_directory:toolDir,
      tooling_package_lock_sha256:sha(path.join(toolDir,'package-lock.json')),
      headless:true, fresh_browser_context:true, project_dependencies_changed:false,
      allowed_views:['home','catalog'], prohibited_actions:['settings navigation/screenshots','execution submission','qualification','source sync','connection tests'],
      invocation_example:'node browser-qa.cjs --mode=capture --url=http://127.0.0.1:8501 --out=<evidence directory>',
    };
    write('browser-environment.json',environment);
    if(mode==='setup') { console.log(JSON.stringify(environment,null,2)); return; }
    if(mode!=='capture') throw new Error('Only setup/capture modes are currently authorized');
    const results=[];
    for(const width of [1440,390]) {
      const context=await browser.newContext({viewport:{width,height:1000},locale:'fa-IR',deviceScaleFactor:1});
      await context.route('**/*',async route=>{
        const request=route.request(); const target=new URL(request.url());
        if(!['127.0.0.1','localhost'].includes(target.hostname)||!['GET','HEAD'].includes(request.method())) return route.abort('blockedbyclient');
        return route.continue();
      });
      const page=await context.newPage(); const errors=[];
      page.on('pageerror',error=>errors.push(String(error)));
      await page.goto(url,{waitUntil:'domcontentloaded',timeout:30000});
      await page.getByRole('heading',{name:'چه مسئله‌ای را می‌خواهید حل کنید؟',exact:true}).waitFor({state:'visible',timeout:30000});
      await page.screenshot({path:path.join(out,`home-${width}.png`),fullPage:true});
      const homeText=await page.locator('body').innerText();
      const homeGeometry=await page.evaluate(()=>({viewport:window.innerWidth,documentWidth:document.documentElement.scrollWidth,bodyWidth:document.body.scrollWidth}));
      if(width<700) await page.getByRole('button',{name:'منو',exact:true}).click();
      const nav=page.getByRole('button',{name:'منابع و الگوریتم‌ها',exact:true});
      await nav.filter({visible:true}).click();
      await page.getByRole('heading',{name:'منابع و الگوریتم‌ها',exact:true}).waitFor({state:'visible'});
      await page.getByRole('tab',{name:'الگوریتم‌ها',exact:true}).click();
      await page.getByRole('heading',{name:'الگوریتم‌ها',exact:true}).waitFor({state:'visible'});
      await page.locator('summary').filter({hasText:'آماده‌سازی نسخهٔ دانشگاهی'}).click();
      await page.locator('summary').filter({hasText:'وضعیت شش روش'}).click();
      await page.screenshot({path:path.join(out,`catalog-${width}.png`),fullPage:true});
      const catalogText=await page.locator('body').innerText();
      const catalogGeometry=await page.evaluate(()=>({viewport:window.innerWidth,documentWidth:document.documentElement.scrollWidth,bodyWidth:document.body.scrollWidth}));
      const result={width,url,home:{text:homeText,geometry:homeGeometry},catalog:{text:catalogText,geometry:catalogGeometry},page_errors:errors,read_only_navigation:true};
      write(`capture-${width}.json`,result);results.push({width,page_errors:errors,homeGeometry,catalogGeometry});
      await context.close();
    }
    write('capture-summary.json',{captured_at_utc:new Date().toISOString(),results,model_calls:0,function_evaluations:0});
    console.log(JSON.stringify(results,null,2));
  } finally { await browser.close(); }
})().catch(error=>{write('capture-error.json',{time:new Date().toISOString(),name:error.name,message:error.message});console.error(error.message);process.exitCode=1;});