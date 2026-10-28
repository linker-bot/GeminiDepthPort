// Exercise the real form handlers without HTTP, cameras, or a browser dependency.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('web/app.js', 'utf8').split("$('camera-form').addEventListener")[0];
async function check(backend) {
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) elements.set(id, {id,value:'',checked:false,hidden:false,dataset:{},classList:{toggle(){}},removeAttribute(){},closest(){return get('exposure-details');}});
    return elements.get(id);
  };
  let saved;
  const inventory = {schema_version:2,capture_backend:backend,cameras:[{name:'cam',enabled:true,serial_number:'SYNTHETIC',health:{},parameters:{enable_color:true,enable_depth:true,color_width:640,color_height:480,color_fps:30,depth_width:640,depth_height:480,depth_fps:30,color_format:'YUYV',depth_format:'Y16'}}]};
  const context = vm.createContext({structuredClone,console,document:{getElementById:get,querySelector:()=>({content:'token'}),querySelectorAll:selector=>selector.startsWith('#camera-form')?[...elements.values()]:[]},fetch:async (path,options)=>{
    if(options.body) saved=JSON.parse(options.body);
    return {ok:true,json:async()=>path==='/api/config'?{message:'saved'}:{inventory,revision:'next',devices:[],previews:{}}};
  }});
  vm.runInContext(source,context);
  context.fixture=inventory;
  vm.runInContext("draft=structuredClone(fixture); state={devices:[],previews:{}}; revision='old'; showRole(); updateButtons();",context);
  assert.equal(get('exposure-details').hidden,backend==='rust');
  assert.equal(get('color_gain').disabled,backend==='rust');
  get('serial').value='NEW_SYNTHETIC'; get('color_width').value='1280';
  vm.runInContext('readForm()',context);
  await vm.runInContext('saveConfig()',context);
  assert.equal(saved.cameras[0].serial_number,'NEW_SYNTHETIC');
  assert.equal(saved.cameras[0].parameters.color_width,1280);
  for(const key of ['color_exposure','color_gain','ir_exposure','ir_gain','enable_color_auto_exposure','enable_ir_auto_exposure']) {
    assert.equal(Object.hasOwn(saved.cameras[0].parameters,key),backend!=='rust');
  }
}
(async()=>{await check('rust');await check('official');console.log('Web form backend regression passed');})().catch(e=>{console.error(e);process.exitCode=1;});
