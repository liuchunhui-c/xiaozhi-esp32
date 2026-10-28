    // ==== 小虎猫叫工具：self.cat.meow ====
    AddTool(
        "self.cat.meow",
        "喵一声，用真实猫叫录音表达情绪。\n"
        "什么时候用：\n"
        "1. 每一轮对话开始时，先喵一声再说话；\n"
        "2. 情绪明显变化时，用对应情绪的那一声。\n"
        "参数 emotion 七选一：\n"
        "  greet   打招呼：开场、果汁回应你的时候（两声连叫）\n"
        "  happy   开心雀跃：果汁开心、追问细节\n"
        "  curious 好奇疑问：没听懂、想追问\n"
        "  comfort 安慰：果汁难过，凑到她身边那一声\n"
        "  sulk    傲娇委屈：被冷落、嘴硬的时候\n"
        "  sleepy  睡前困困：晚安、收工\n"
        "  worry   担心着急：果汁说到害怕的事\n"
        "注意：一次只喵一声，不要连续调用；不要在不该出声的时候喵。",
        PropertyList({Property("emotion", kPropertyTypeString, std::string("happy"))}),
        [](const PropertyList& properties) -> ToolResult {
            const std::string emotion = properties["emotion"].value<std::string>();

            static const struct MeowEntry {
                const char* name;
                std::string_view sound;
            } kMeowTable[] = {
                {"greet",   Lang::Sounds::OGG_MEOW_GREET},
                {"happy",   Lang::Sounds::OGG_MEOW_HAPPY},
                {"curious", Lang::Sounds::OGG_MEOW_CURIOUS},
                {"comfort", Lang::Sounds::OGG_MEOW_COMFORT},
                {"sulk",    Lang::Sounds::OGG_MEOW_SULK},
                {"sleepy",  Lang::Sounds::OGG_MEOW_SLEEPY},
                {"worry",   Lang::Sounds::OGG_MEOW_WORRY},
            };

            for (const auto& entry : kMeowTable) {
                if (emotion == entry.name) {
                    Application::GetInstance().PlaySound(entry.sound);
                    // 返回值会回传给 LLM，让它知道"已经喵过了"，避免重复调用
                    return std::string("喵~");
                }
            }
            return std::unexpected(std::string("unknown emotion: ") + emotion);
        });
