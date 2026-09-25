import { app } from "../utils/comfyapp"
import { useMemo } from "react";

const STAR_NUM = 3000

export interface ShowcaseItem {
    name: string;
    // The debug chip triggers the same "would you like me to debug this workflow" flow as the
    // bug icon in ChatInput, instead of sending its label as a chat message.
    isDebug?: boolean;
}

// These used to be public/showcase/{showcase,showcase_en}.json, each entry pairing a label with
// a pre-recorded fake conversation that Showcase.tsx replayed as a scripted "streaming" demo.
// The chips now perform the real action their label describes (see Showcase.tsx), so only the
// labels remain.
const SHOWCASE_ITEMS_EN: ShowcaseItem[] = [
    { name: 'Give me a workflow to create a short video clip based on ltx' },
    { name: 'Give me a workflow that can turn the image into anime style' },
    { name: 'Give me a workflow for image editing' },
    { name: 'rewrite current workflow, cut out the cat in my output image' },
    { name: 'debug the workflow of the current canvas', isDebug: true },
    { name: 'Give me a node to merge multiple images' },
    { name: 'Give me a Studio Ghibli style LoRA' },
    { name: 'Generate prompt based on my description' },
    { name: 'Generate 3 prompts based on my requirements and test each with image generation' },
];

const SHOWCASE_ITEMS_ZH: ShowcaseItem[] = [
    { name: '给我一个工作流，基于ltx创建一个短视频' },
    { name: '给我一个工作流，把图片变成动漫风格' },
    { name: '给我一个编辑图片的工作流' },
    { name: '修改当前工作流，检测并裁剪出图片中的猫' },
    { name: '调试当前工作流', isDebug: true },
    { name: '给我一个把多张图合并的节点' },
    { name: '给我一个宫崎骏风格的lora' },
    { name: '基于我的描述生成prompt' },
    { name: '根据我的诉求生成3个prompt，各自生图查看效果' },
];

const useLanguage = () => {
    const language = app.extensionManager.setting.get('Comfy.Locale')

    const languageData = useMemo(() => {
        let showcase_title = ''
        let showcase_subtitle = ''
        let showcase_list: ShowcaseItem[] = SHOWCASE_ITEMS_EN
        let apikeymodel_title = ''
        let chatinput_title = ''
        let startpopview_title = ''
        let startpopview_join = ''
        switch (language) {
          case 'zh':
            showcase_title = '欢迎使用ComfyUI Copilot!'
            showcase_subtitle = `已有 ${STAR_NUM}+ 开发者加入🚀，您的Star是我们持续维护和升级的动力， 👉🏻立即Star。`
            showcase_list = SHOWCASE_ITEMS_ZH
            apikeymodel_title = '🌟 免费羊毛可持续薅，点个Star服务器不跑路！每个Star都是我们续命的氧气！'
            chatinput_title = '您的Star=我们的动力'
            startpopview_title = `加入我们由 ${STAR_NUM}+ 位 Star 支持者组成的大家庭 \n 您的 Star 让我们更强大！`
            startpopview_join = '点赞加入我们！'
            break;
          case 'en':
          default:
            showcase_title = 'Welcome to ComfyUI Copilot!'
            showcase_subtitle = `${STAR_NUM}+ developers joined🚀, Star us to support continuous updates, 👉🏻Star now.`
            showcase_list = SHOWCASE_ITEMS_EN
            apikeymodel_title = '💖 Every ⭐ is our lifeline! Tap that star button to keep the magic alive!'
            chatinput_title = 'Your Star = Our Power'
            startpopview_title = `Join our family of ${STAR_NUM}+ Star supporiters \n Your Star makes us stronger!`
            startpopview_join = 'Join Us! Star Now!'
            break;
        }
        return {
          showcase_title,
          showcase_subtitle,
          showcase_list,
          apikeymodel_title,
          chatinput_title,
          startpopview_title,
          startpopview_join
        };
    }, [language])

    return languageData
}

export default useLanguage