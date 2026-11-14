const fs=require('fs'),path=require('path'),os=require('os'),crypto=require('crypto');
const {pathToFileURL}=require('url');
const toolDir=path.join(os.tmpdir(),'solverscout-windows-rc2-browser-20261114T164636Z');
const {chromium}=require(path.join(toolDir,'node_modules','playwright-core'));
const scenario=process.argv[2]||'U2'; if(!['U1','U2'].includes(scenario)) throw Error('Unknown scenario');
const root=path.resolve('artifacts/university-delivery/20261114T164636Z-windows-rc2');
const out=path.join(root,'browser-qa'); const reportPath=path.join(root,'live',scenario,'report.json');
const htmlPath=path.join(root,'live',scenario,'report.html');const report=JSON.parse(fs.readFileSync(reportPath,'utf8'));
const sha=file=>crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
(async()=>{const browser=await chromium.launch({executablePath:'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',headless:true});
try{const results=[];for(const width of [1440,390]){
 const context=await browser.newContext({viewport:{width,height:1000},locale:'fa-IR',deviceScaleFactor:1});
 const blocked=[],requests=[],errors=[];
 await context.route('**/*',route=>{const req=route.request(); if(!['file:','data:','about:'].includes(new URL(req.url()).protocol)||!['GET','HEAD'].includes(req.method())){blocked.push({url:req.url(),method:req.method()});return route.abort();}return route.continue();});
 const page=await context.newPage();page.on('request',r=>requests.push({url:r.url(),method:r.method()}));page.on('pageerror',e=>errors.push(e.message));
 await page.goto(pathToFileURL(htmlPath).href,{waitUntil:'load'});
 const paragraphs=[];
 for(const section of report.narrative_v3.rendered_sections){for(const paragraph of section.paragraphs){
  const locator=page.locator(`section[data-section="${section.section_id}"] p[data-paragraph="${paragraph.paragraph_id}"]`);
  const count=await locator.count();const actual=count===1?await locator.textContent():null;const visible=count===1?await locator.isVisible():false;
  paragraphs.push({section_id:section.section_id,paragraph_id:paragraph.paragraph_id,element_count:count,exact_match:actual===paragraph.text_fa,visible,unresolved_fact_token:!!actual&&/\{\{fact:|\}\}/.test(actual)});
 }}
 const geometry=await page.evaluate(()=>({viewport_width:innerWidth,document_width:document.documentElement.scrollWidth,body_width:document.body.scrollWidth,narrative_font_size:getComputedStyle(document.querySelector('.narrative-v3')).fontSize,narrative_line_height:getComputedStyle(document.querySelector('.narrative-v3')).lineHeight,document_direction:document.documentElement.dir,narrative_direction:getComputedStyle(document.querySelector('.narrative-v3')).direction,section_count:document.querySelectorAll('section[data-section]').length,scroll_containers:[...document.querySelectorAll('.table-scroll,[data-block="progress"],.math-line')].filter(e=>e.getClientRects().length).map(e=>({client_width:e.clientWidth,scroll_width:e.scrollWidth,overflow_x:getComputedStyle(e).overflowX}))}));
 const body=await page.locator('body').innerText(); const paragraphText=await page.locator('section[data-section] p[data-paragraph]').allTextContents();
 await page.screenshot({path:path.join(out,`${scenario}-html-${width}.png`),fullPage:true});
 await page.screenshot({path:path.join(out,`${scenario}-html-${width}-viewport.png`),fullPage:false});
 await page.locator('section[data-section="candidate_rationale"]').screenshot({path:path.join(out,`${scenario}-html-${width}-candidate-rationale.png`)});
 await page.locator('section[data-section="verification_and_limits"]').screenshot({path:path.join(out,`${scenario}-html-${width}-verification.png`)});
 const modelLabelPresent=body.includes('مدل');const originLabelPresent=body.includes('منشأ')&&body.includes('اجرای اصلی');
 const numericalEvidence={objective_raw:report.objective_raw,best_x:report.best_x,solution_origin:report.solution_origin,selected_algorithm_id:report.selected_algorithm_id,solution_paragraph_exact_match:paragraphs.filter(x=>x.section_id==='solution').every(x=>x.exact_match),verification_paragraphs_exact_match:paragraphs.filter(x=>x.section_id==='verification_and_limits').every(x=>x.exact_match)};
 const item={width,paragraph_checks:paragraphs,geometry,page_errors:errors,requests,blocked_requests:blocked,numerical_evidence:numericalEvidence,model_label_present:modelLabelPresent,origin_label_present:originLabelPresent,body_text_sha256:crypto.createHash('sha256').update(body).digest('hex'),unresolved_tokens_in_narrative:paragraphText.some(t=>/\{\{fact:|\}\}/.test(t)),passed:paragraphs.every(x=>x.exact_match&&x.visible&&!x.unresolved_fact_token)&&geometry.section_count===5&&geometry.document_width<=width&&errors.length===0&&blocked.length===0&&modelLabelPresent&&originLabelPresent};
 fs.writeFileSync(path.join(out,`${scenario}-html-${width}.json`),JSON.stringify(item,null,2)+'\n');results.push(item);await context.close();
 }
 const summary={schema_version:'saved-html-browser-audit/1.0',captured_at_utc:new Date().toISOString(),scenario,job_id:report.job_id,html_path:htmlPath,html_sha256:sha(htmlPath),report_json_sha256:sha(reportPath),browser_version:browser.version(),narrative_mode:report.narrative_v3.mode,narrative_review_status:report.narrative_v3.review_status,section_count:report.narrative_v3.rendered_sections.length,paragraph_count:report.narrative_v3.rendered_sections.reduce((n,s)=>n+s.paragraphs.length,0),passed:results.every(r=>r.passed),widths:results.map(r=>({width:r.width,passed:r.passed,document_width:r.geometry.document_width,exact_paragraph_matches:r.paragraph_checks.filter(p=>p.exact_match).length,page_errors:r.page_errors,external_or_mutating_requests:r.blocked_requests.length})),function_evaluations:0,model_calls:0};
 fs.writeFileSync(path.join(out,`${scenario}-html-summary.json`),JSON.stringify(summary,null,2)+'\n');console.log(JSON.stringify(summary,null,2));
}finally{await browser.close();}})().catch(e=>{console.error(e.stack);process.exitCode=1});